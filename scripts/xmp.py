"""Export a recipe/style as a Lightroom Classic / Camera Raw preset (.xmp)."""
from __future__ import annotations

import uuid
from xml.sax.saxutils import escape

BANDS = {"red": "Red", "orange": "Orange", "yellow": "Yellow", "green": "Green",
         "aqua": "Aqua", "blue": "Blue", "purple": "Purple", "magenta": "Magenta"}


def _s(v, dec=0):
    v = round(float(v), dec)
    if dec == 0:
        v = int(v)
        return f"+{v}" if v > 0 else str(v)
    return f"+{v:.{dec}f}" if v > 0 else f"{v:.{dec}f}"


def recipe_to_xmp(r, name, group="Photo Edit Skill"):
    a = {}
    simple = {"exposure": ("Exposure2012", 2), "contrast": ("Contrast2012", 0), "highlights": ("Highlights2012", 0),
              "shadows": ("Shadows2012", 0), "whites": ("Whites2012", 0), "blacks": ("Blacks2012", 0),
              "texture": ("Texture", 0), "clarity": ("Clarity2012", 0), "dehaze": ("Dehaze", 0),
              "vibrance": ("Vibrance", 0), "saturation": ("Saturation", 0),
              "temp": ("IncrementalTemperature", 0), "tint": ("IncrementalTint", 0)}
    for k, (tag, dec) in simple.items():
        if k in r:
            a[tag] = _s(r[k], dec)
    if "temp" in r or "tint" in r:
        a["WhiteBalance"] = "Custom"
    for b, B in BANDS.items():
        d = (r.get("hsl") or {}).get(b, {})
        if d:
            a[f"HueAdjustment{B}"] = _s(d.get("hue", 0))
            a[f"SaturationAdjustment{B}"] = _s(d.get("sat", 0))
            a[f"LuminanceAdjustment{B}"] = _s(d.get("lum", 0))
    cg = r.get("color_grade") or {}
    if cg:
        sh, mid, hi, gl = (cg.get(k, {}) for k in ("shadows", "midtones", "highlights", "global"))
        a.update({
            "SplitToningShadowHue": str(int(sh.get("hue", 0))), "SplitToningShadowSaturation": str(int(sh.get("sat", 0))),
            "SplitToningHighlightHue": str(int(hi.get("hue", 0))), "SplitToningHighlightSaturation": str(int(hi.get("sat", 0))),
            "SplitToningBalance": _s(cg.get("balance", 0)),
            "ColorGradeMidtoneHue": str(int(mid.get("hue", 0))), "ColorGradeMidtoneSat": str(int(mid.get("sat", 0))),
            "ColorGradeShadowLum": _s(sh.get("lum", 0)), "ColorGradeMidtoneLum": _s(mid.get("lum", 0)),
            "ColorGradeHighlightLum": _s(hi.get("lum", 0)),
            "ColorGradeGlobalHue": str(int(gl.get("hue", 0))), "ColorGradeGlobalSat": str(int(gl.get("sat", 0))),
            "ColorGradeGlobalLum": _s(gl.get("lum", 0)),
            "ColorGradeBlending": str(int(cg.get("blending", 50))),
        })
    bw = r.get("bw") or {}
    if bw.get("enabled"):
        a["ConvertToGrayscale"] = "True"
        for b, B in BANDS.items():
            a[f"GrayMixer{B}"] = _s((bw.get("mix") or {}).get(b, 0))
    sh = r.get("sharpen") or {}
    if sh:
        a.update({"Sharpness": str(int(sh.get("amount", 40))), "SharpenRadius": _s(sh.get("radius", 1.0), 1),
                  "SharpenDetail": "25", "SharpenEdgeMasking": str(int(sh.get("masking", 0)))})
    nr = r.get("noise") or {}
    if nr:
        a["LuminanceSmoothing"] = str(int(nr.get("luminance", 0)))
        a["ColorNoiseReduction"] = str(int(nr.get("color", 25)))
    vg = r.get("vignette") or {}
    if vg.get("amount"):
        a.update({"PostCropVignetteStyle": "1", "PostCropVignetteAmount": _s(vg["amount"]),
                  "PostCropVignetteMidpoint": str(int(vg.get("midpoint", 50))),
                  "PostCropVignetteFeather": str(int(vg.get("feather", 50))),
                  "PostCropVignetteRoundness": "0", "PostCropVignetteHighlightContrast": "0"})
    gr = r.get("grain") or {}
    if gr.get("amount"):
        a.update({"GrainAmount": str(int(gr["amount"])), "GrainSize": str(int(gr.get("size", 25))),
                  "GrainFrequency": str(int(gr.get("roughness", 50)))})

    curves = r.get("tone_curve") or {}
    seqs = ""
    for ch, tag in (("master", "ToneCurvePV2012"), ("red", "ToneCurvePV2012Red"),
                    ("green", "ToneCurvePV2012Green"), ("blue", "ToneCurvePV2012Blue")):
        pts = curves.get(ch) or [[0, 0], [255, 255]]
        lis = "".join(f"\n     <rdf:li>{int(round(x))}, {int(round(y))}</rdf:li>" for x, y in pts)
        seqs += f"\n   <crs:{tag}>\n    <rdf:Seq>{lis}\n    </rdf:Seq>\n   </crs:{tag}>"
    a["ToneCurveName2012"] = "Custom" if curves else "Linear"

    attrs = "\n".join(f'   crs:{k}="{escape(v)}"' for k, v in a.items())
    desc = escape(r.get("description", ""))
    return f"""<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0-c000 1.000000, 0000/00/00-00:00:00        ">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:PresetType="Normal"
   crs:Cluster=""
   crs:UUID="{uuid.uuid4().hex.upper()}"
   crs:SupportsAmount="True"
   crs:SupportsAmount2="True"
   crs:SupportsColor="True"
   crs:SupportsMonochrome="True"
   crs:SupportsHighDynamicRange="True"
   crs:SupportsNormalDynamicRange="True"
   crs:SupportsSceneReferred="True"
   crs:SupportsOutputReferred="True"
   crs:CameraModelRestriction=""
   crs:Copyright=""
   crs:ContactInfo=""
   crs:Version="15.0"
   crs:ProcessVersion="11.0"
{attrs}
   crs:HasSettings="True">
   <crs:Name>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{escape(name)}</rdf:li>
    </rdf:Alt>
   </crs:Name>
   <crs:ShortName>
    <rdf:Alt>
     <rdf:li xml:lang="x-default"/>
    </rdf:Alt>
   </crs:ShortName>
   <crs:SortName>
    <rdf:Alt>
     <rdf:li xml:lang="x-default"/>
    </rdf:Alt>
   </crs:SortName>
   <crs:Group>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{escape(group)}</rdf:li>
    </rdf:Alt>
   </crs:Group>
   <crs:Description>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{desc}</rdf:li>
    </rdf:Alt>
   </crs:Description>{seqs}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""
