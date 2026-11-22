// Extract the segmentation mattes an iPhone writes into its HEIC/JPG (ImageIO auxiliary data):
// portrait (person), skin, hair, teeth, glasses, sky -> 8-bit grayscale PNGs.
// usage: swift apple_mattes.swift IMAGE OUT_DIR      prints the mattes it found
import Foundation
import ImageIO

let args = CommandLine.arguments
guard args.count >= 3, let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[1]) as CFURL, nil) else {
    FileHandle.standardError.write("usage: apple_mattes.swift IMAGE OUT_DIR\n".data(using: .utf8)!); exit(2)
}
let outDir = URL(fileURLWithPath: args[2])
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
let types: [(String, CFString)] = [
    ("portrait", kCGImageAuxiliaryDataTypePortraitEffectsMatte),
    ("skin", kCGImageAuxiliaryDataTypeSemanticSegmentationSkinMatte),
    ("hair", kCGImageAuxiliaryDataTypeSemanticSegmentationHairMatte),
    ("teeth", kCGImageAuxiliaryDataTypeSemanticSegmentationTeethMatte),
    ("glasses", kCGImageAuxiliaryDataTypeSemanticSegmentationGlassesMatte),
    ("sky", kCGImageAuxiliaryDataTypeSemanticSegmentationSkyMatte),
]
var found: [String] = []
for (name, t) in types {
    guard let info = CGImageSourceCopyAuxiliaryDataInfoAtIndex(src, 0, t) as? [AnyHashable: Any],
          let data = info[kCGImageAuxiliaryDataInfoData] as? Data,
          let desc = info[kCGImageAuxiliaryDataInfoDataDescription] as? [AnyHashable: Any],
          let w = desc["Width"] as? Int, let h = desc["Height"] as? Int, let bpr = desc["BytesPerRow"] as? Int,
          let prov = CGDataProvider(data: data as CFData),
          let img = CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: bpr,
                            space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: 0),
                            provider: prov, decode: nil, shouldInterpolate: false, intent: .defaultIntent),
          let dst = CGImageDestinationCreateWithURL(outDir.appendingPathComponent("\(name).png") as CFURL,
                                                    "public.png" as CFString, 1, nil)
    else { continue }
    CGImageDestinationAddImage(dst, img, nil)
    if CGImageDestinationFinalize(dst) { found.append(name) }
}
let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [AnyHashable: Any]
let orient = props?[kCGImagePropertyOrientation] as? Int ?? 1
// mattes are stored in sensor orientation; record the EXIF orientation so the reader can rotate them upright
let meta = "{\"orientation\": \(orient), \"mattes\": [\(found.map { "\"\($0)\"" }.joined(separator: ", "))]}"
try? meta.write(to: outDir.appendingPathComponent("meta.json"), atomically: true, encoding: .utf8)
print(found.joined(separator: " "), "orientation=\(orient)")
