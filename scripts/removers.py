"""Effect-aware object removal backends (diffusion), used by erase.py.

Backends (pick with `--backend`, default `auto`):
  osor         OSOR-SDXL-Inpainting, one-step, removes the person *and* their shadow/reflection.
               Code MIT (github.com/Zhouqm-Git/osor); weights CreativeML Open RAIL++-M
               (inherits SDXL-Inpainting 0.1). Commercial use allowed under RAIL++-M use limits.
  objectclear  ObjectClear (CVPR 2026, S-Lab NTU), SDXL-based, 20 steps, most precise shadow
               removal. S-Lab License 1.0 -> NON-COMMERCIAL use only. The repo is cloned on
               first use into the model cache (it has no pip package).
  lama         big-lama (Apache-2.0), the original fast fallback; no shadow removal.
  auto         osor on CUDA when diffusers+peft are installed, else lama.

Both diffusion models were trained at 512 px, so every removal region is cropped with
context, run at ~512 px short side, scaled back, and only the removed area (person + the
effect area the model reports) is pasted over the original. Kept subjects are always
protected from the paste, and full-resolution pixels outside the paste stay untouched.

Requires (in addition to torch + ultralytics):
  diffusers==0.32.2 transformers peft==0.14.0 accelerate safetensors huggingface_hub
  (+ scipy for objectclear). First run downloads ~7 GB of SDXL-Inpainting fp16 weights.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import cv2
import numpy as np

BACKENDS = ("auto", "osor", "objectclear", "lama")
SDXL_INPAINT = "diffusers/stable-diffusion-xl-1.0-inpainting-0.1"
OSOR_REPO, OSOR_FILE = "QinmingZhou/OSOR", "osor-sdxlinpainting/weights/sdxlinpainting_phase2.pth"
OBJECTCLEAR_REPO = "jixin0101/ObjectClear"
OBJECTCLEAR_GIT = "https://github.com/zjx0101/ObjectClear"
_MODELS = {}


def diffusion_ready():
    import torch
    return torch.cuda.is_available() and all(
        importlib.util.find_spec(n) is not None for n in ("diffusers", "peft", "transformers", "huggingface_hub"))


def resolve(backend):
    backend = (backend or "auto").lower()
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; choose from {', '.join(BACKENDS)}")
    if backend == "auto":
        return "osor" if diffusion_ready() else "lama"
    return backend


# ----------------------------------------------------------------------------- region pipeline
def _regions(mask):
    """Crop boxes around mask components: generous context, extra room below (shadows)."""
    h, w = mask.shape
    n, _, st, _ = cv2.connectedComponentsWithStats((mask > 127).astype(np.uint8))
    boxes = []
    for i in range(1, n):
        x, y, bw, bh, a = st[i]
        if a < 16:
            continue
        p = int(max(bw, bh) * 0.6 + 32)
        boxes.append([max(0, x - p), max(0, y - p), min(w, x + bw + p), min(h, y + bh + int(p * 1.4))])
    merged = []
    for b in sorted(boxes):
        for m in merged:
            if b[0] < m[2] and b[2] > m[0] and b[1] < m[3] and b[3] > m[1]:
                m[:] = [min(m[0], b[0]), min(m[1], b[1]), max(m[2], b[2]), max(m[3], b[3])]
                break
        else:
            merged.append(list(b))
    return merged


def _work_size(cw, ch, short=512, long_cap=1024):
    s = short / min(cw, ch)
    if max(cw, ch) * s > long_cap:
        s = long_cap / max(cw, ch)
    return max(16, int(round(cw * s / 16)) * 16), max(16, int(round(ch * s / 16)) * 16)


def _color_fix(filled, crop, alpha):
    """Shift the fill so its median matches the original on a ring just outside the paste."""
    a = alpha > 0.05
    ring = (cv2.dilate(a.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0) & ~a
    if ring.sum() < 50:
        return filled
    return filled + np.median(crop[ring] - filled[ring], axis=0)[None, None, :]


def _grain(filled, crop, alpha, scale):
    """Diffusion output is upscaled from ~512 px: add noise matched to the photo's own grain."""
    if scale >= 1:
        return filled
    hf = crop - cv2.GaussianBlur(crop, (0, 0), 1.0 / scale)
    keep = alpha < 0.05
    sd = float(np.median(np.abs(hf[keep]))) * 1.4 if keep.any() else 0.0
    noise = cv2.GaussianBlur(np.random.default_rng(0).standard_normal(filled.shape[:2]).astype(np.float32), (0, 0), 0.6)
    return filled + (noise / max(noise.std(), 1e-6) * min(sd, 0.02) * 0.6)[..., None]


def remove(img, mask, backend, protect=None):
    """img float sRGB HxWx3 0..1; mask uint8 (255 = object, near-tight); protect uint8 (255 = never touch).
    Returns the image with each region replaced by the backend's effect-aware fill."""
    run = {"osor": _osor, "objectclear": _objectclear}[backend]
    out = img.copy()
    for x0, y0, x1, y1 in _regions(mask):
        crop = out[y0:y1, x0:x1]
        cm = mask[y0:y1, x0:x1]
        ch, cw = cm.shape
        W, H = _work_size(cw, ch)
        small = cv2.resize((np.clip(crop, 0, 1) * 255).astype(np.uint8), (W, H), interpolation=cv2.INTER_AREA)
        smask = cv2.resize(cm, (W, H), interpolation=cv2.INTER_NEAREST)
        res, eff = run(small, smask)
        res = cv2.resize(res.astype(np.float32), (cw, ch), interpolation=cv2.INTER_CUBIC)
        base = (cm > 127).astype(np.float32)
        if eff is not None:  # shadow / reflection area the model removed too
            base = np.maximum(base, cv2.resize(eff.astype(np.float32), (cw, ch), interpolation=cv2.INTER_LINEAR))
        r = max(2, int(0.006 * max(cw, ch)))
        base = cv2.dilate(base, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1,) * 2))
        alpha = np.clip(cv2.GaussianBlur(base, (0, 0), max(1.5, r * 0.8)), 0, 1)
        if protect is not None:  # kept subjects: no soft edge eating into hair / ears
            pm = cv2.GaussianBlur((protect[y0:y1, x0:x1] > 127).astype(np.float32), (0, 0), 1.0)
            alpha = alpha * (1 - pm)
        res = _grain(_color_fix(res, crop, alpha), crop, alpha, min(1.0, W / cw))
        out[y0:y1, x0:x1] = crop * (1 - alpha[..., None]) + np.clip(res, 0, 1) * alpha[..., None]
    return np.clip(out, 0, 1)


# ----------------------------------------------------------------------------- OSOR (MIT code, RAIL++-M weights)
# Minimal re-implementation of OSOR's SDXL Phase-II generator + one-step inference
# (src/models/generator*.py and scripts/inference_enhance.py of github.com/Zhouqm-Git/osor, MIT).
OSOR_LORA_MODULES = ["to_k", "to_q", "to_v", "to_out.0", "conv", "conv1", "conv2", "conv_shortcut",
                     "proj_in", "proj_out", "ff.net.2", "ff.net.0.proj"]
OSOR_PROMPT = "Remove the instance of object"


def _sdxl_dir():
    from huggingface_hub import snapshot_download
    return snapshot_download(SDXL_INPAINT, allow_patterns=[
        "*.json", "*.txt", "unet/*.fp16.safetensors", "vae/*.fp16.safetensors", "text_encoder*/*.fp16.safetensors"])


def _osor_prompt(base, dev, dtype):
    from erase import MODEL_DIR
    import torch
    cache = MODEL_DIR / "osor_prompt_embeds.pt"
    if not cache.exists():
        from transformers import CLIPTextModel, CLIPTextModelWithProjection, CLIPTokenizer
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        ids = lambda tok: tok(OSOR_PROMPT, padding="max_length", max_length=77, truncation=True,
                              return_tensors="pt").input_ids.to(dev)
        t1 = CLIPTokenizer.from_pretrained(base, subfolder="tokenizer")
        e1 = CLIPTextModel.from_pretrained(base, subfolder="text_encoder", variant="fp16", torch_dtype=torch.float16).to(dev)
        t2 = CLIPTokenizer.from_pretrained(base, subfolder="tokenizer_2")
        e2 = CLIPTextModelWithProjection.from_pretrained(base, subfolder="text_encoder_2", variant="fp16",
                                                         torch_dtype=torch.float16).to(dev)
        with torch.no_grad():
            h1 = e1(ids(t1), output_hidden_states=True).hidden_states[-2]
            o2 = e2(ids(t2), output_hidden_states=True)
        torch.save({"prompt_embeds": torch.cat([h1, o2.hidden_states[-2]], -1).cpu(),
                    "pooled_prompt_embeds": o2.text_embeds.cpu()}, cache)
        del e1, e2
        torch.cuda.empty_cache()
    d = torch.load(cache, map_location="cpu")
    return d["prompt_embeds"].to(dev, dtype), d["pooled_prompt_embeds"].to(dev, dtype)


def _osor_load():
    import torch
    import torch.nn as nn
    from diffusers import AutoencoderKL, DDPMScheduler, UNet2DConditionModel
    from huggingface_hub import hf_hub_download
    from peft import LoraConfig

    dev, dtype = "cuda", torch.bfloat16
    base = _sdxl_dir()

    class Generator(nn.Module):  # attribute name `unet` must match the checkpoint keys
        def __init__(self):
            super().__init__()
            self.unet = UNet2DConditionModel.from_pretrained(base, subfolder="unet", variant="fp16", torch_dtype=dtype)
            self.unet.add_adapter(LoraConfig(r=256, lora_alpha=256, init_lora_weights="gaussian",
                                             target_modules=OSOR_LORA_MODULES))
            old = self.unet.conv_out  # 4 denoise channels + 1 alpha (effect mask) logit
            new = nn.Conv2d(old.in_channels, 5, old.kernel_size, old.stride, old.padding)
            self.unet.conv_out = new

    g = Generator()
    state = torch.load(hf_hub_download(OSOR_REPO, OSOR_FILE), map_location="cpu", weights_only=True)
    state = {k.replace("module.", "").replace("_orig_mod.", ""): v for k, v in state.items()}
    res = g.load_state_dict(state, strict=False)
    if res.unexpected_keys:  # LoRA key names depend on the peft version
        raise RuntimeError(f"OSOR checkpoint does not match this diffusers/peft version "
                           f"(e.g. {res.unexpected_keys[:2]}); use diffusers==0.32.2 peft==0.14.0")
    g = g.to(dev, dtype).eval().requires_grad_(False)
    vae = AutoencoderKL.from_pretrained(base, subfolder="vae", variant="fp16", torch_dtype=dtype).to(dev).eval()
    sched = DDPMScheduler.from_pretrained(base, subfolder="scheduler")
    pe, pooled = _osor_prompt(base, dev, dtype)
    return g, vae, sched, pe, pooled


def _osor(small, smask, t=400):
    import torch
    import torch.nn.functional as F
    if "osor" not in _MODELS:
        _MODELS["osor"] = _osor_load()
    g, vae, sched, pe, pooled = _MODELS["osor"]
    dtype = vae.dtype
    H, W = smask.shape
    with torch.no_grad():
        x = torch.from_numpy(small).permute(2, 0, 1)[None].float().div(255).cuda() * 2 - 1
        m = torch.from_numpy((smask > 127).astype(np.float32))[None, None].cuda()
        z = vae.encode(x.to(dtype)).latent_dist.sample(torch.Generator("cuda").manual_seed(0)) * vae.config.scaling_factor
        ml = F.max_pool2d(m, 8, 8).to(dtype)
        noise = torch.randn(z.shape, generator=torch.Generator("cuda").manual_seed(42), device="cuda", dtype=dtype)
        ts = torch.full((1,), t, dtype=torch.long, device="cuda")
        zt = sched.add_noise(z, noise, ts)
        cond = {"text_embeds": pooled, "time_ids": torch.tensor([[H, W, 0, 0, H, W]], dtype=dtype, device="cuda")}
        o = g.unet(torch.cat([zt, ml, z], 1), ts, encoder_hidden_states=pe, added_cond_kwargs=cond, return_dict=False)[0]
        alpha = torch.sigmoid(o[:, 4:])
        z0 = sched.step(o[:, :4], ts, zt).pred_original_sample
        z0 = z0 * alpha + z * (1 - alpha)
        y = vae.decode(z0.to(dtype) / vae.config.scaling_factor).sample.float()
        y = ((y + 1) / 2).clamp(0, 1)[0].permute(1, 2, 0).cpu().numpy()
        a = F.interpolate(alpha.float(), size=(H, W), mode="bilinear")[0, 0].cpu().numpy()
    return y, (a > 0.5).astype(np.float32)


# ----------------------------------------------------------------------------- ObjectClear (S-Lab, non-commercial)
def _objectclear_load():
    import torch
    from erase import MODEL_DIR
    repo = MODEL_DIR / "ObjectClear"
    if not (repo / "objectclear").exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        print(f"cloning {OBJECTCLEAR_GIT} -> {repo} (one-time; S-Lab License 1.0, non-commercial)", flush=True)
        subprocess.run(["git", "clone", "--depth", "1", OBJECTCLEAR_GIT, str(repo)], check=True)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from objectclear.pipelines import ObjectClearPipeline
    return ObjectClearPipeline.from_pretrained_with_custom_modules(
        OBJECTCLEAR_REPO, torch_dtype=torch.float16, apply_attention_guided_fusion=False, variant="fp16").to("cuda")


def _objectclear(small, smask, steps=20, guidance=2.5):
    import torch
    from PIL import Image
    if "objectclear" not in _MODELS:
        _MODELS["objectclear"] = _objectclear_load()
    pipe = _MODELS["objectclear"]
    H, W = smask.shape
    r = pipe(prompt="remove the instance of object", image=Image.fromarray(small), mask_image=Image.fromarray(smask),
             generator=torch.Generator("cuda").manual_seed(42), num_inference_steps=steps,
             guidance_scale=guidance, height=H, width=W, return_attn_map=True)
    res = np.asarray(r.images[0].resize((W, H))).astype(np.float32) / 255
    eff = None
    if r.attns:
        eff = (np.asarray(r.attns[0].resize((W, H), Image.BILINEAR)).astype(np.float32) / 255 > 0.5).astype(np.float32)
    return res, eff
