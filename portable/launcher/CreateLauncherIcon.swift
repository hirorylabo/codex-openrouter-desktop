import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: CreateLauncherIcon OUTPUT.png\n".utf8))
    exit(2)
}

let dimension = 1024
let colorSpace = CGColorSpaceCreateDeviceRGB()
guard let context = CGContext(
    data: nil,
    width: dimension,
    height: dimension,
    bitsPerComponent: 8,
    bytesPerRow: dimension * 4,
    space: colorSpace,
    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
) else {
    FileHandle.standardError.write(Data("cannot create icon canvas\n".utf8))
    exit(1)
}
context.setAllowsAntialiasing(true)
context.setShouldAntialias(true)

let background = CGPath(
    roundedRect: CGRect(x: 52, y: 52, width: 920, height: 920),
    cornerWidth: 212,
    cornerHeight: 212,
    transform: nil
)
context.addPath(background)
context.clip()
let gradient = CGGradient(
    colorsSpace: colorSpace,
    colors: [
        CGColor(red: 0.09, green: 0.11, blue: 0.18, alpha: 1),
        CGColor(red: 0.035, green: 0.045, blue: 0.075, alpha: 1),
    ] as CFArray,
    locations: [0, 1]
)!
context.drawLinearGradient(
    gradient,
    start: CGPoint(x: 140, y: 900),
    end: CGPoint(x: 880, y: 120),
    options: []
)
context.resetClip()

context.addPath(CGPath(
    roundedRect: CGRect(x: 72, y: 72, width: 880, height: 880),
    cornerWidth: 192,
    cornerHeight: 192,
    transform: nil
))
context.setStrokeColor(CGColor(gray: 1, alpha: 0.10))
context.setLineWidth(8)
context.strokePath()

let route = CGMutablePath()
route.move(to: CGPoint(x: 276, y: 696))
route.addCurve(
    to: CGPoint(x: 571, y: 512),
    control1: CGPoint(x: 440, y: 696),
    control2: CGPoint(x: 416, y: 512)
)
route.addLine(to: CGPoint(x: 748, y: 512))
route.move(to: CGPoint(x: 276, y: 328))
route.addCurve(
    to: CGPoint(x: 571, y: 512),
    control1: CGPoint(x: 440, y: 328),
    control2: CGPoint(x: 416, y: 512)
)
context.addPath(route)
context.setLineWidth(72)
context.setLineCap(.round)
context.setLineJoin(.round)
context.setStrokeColor(CGColor(red: 0.48, green: 0.55, blue: 1, alpha: 1))
context.strokePath()

func circle(center: CGPoint, radius: CGFloat, color: CGColor, coreRadius: CGFloat) {
    context.setFillColor(color)
    context.fillEllipse(in: CGRect(
        x: center.x - radius, y: center.y - radius,
        width: radius * 2, height: radius * 2
    ))
    context.setFillColor(CGColor(gray: 1, alpha: 0.96))
    context.fillEllipse(in: CGRect(
        x: center.x - coreRadius, y: center.y - coreRadius,
        width: coreRadius * 2, height: coreRadius * 2
    ))
}

circle(
    center: CGPoint(x: 276, y: 696), radius: 92,
    color: CGColor(red: 0.39, green: 0.90, blue: 1, alpha: 1), coreRadius: 36
)
circle(
    center: CGPoint(x: 276, y: 328), radius: 92,
    color: CGColor(red: 0.48, green: 0.55, blue: 1, alpha: 1), coreRadius: 36
)
circle(
    center: CGPoint(x: 748, y: 512), radius: 112,
    color: CGColor(red: 0.82, green: 0.42, blue: 1, alpha: 1), coreRadius: 44
)

let output = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = context.makeImage(),
      let destination = CGImageDestinationCreateWithURL(
          output as CFURL,
          UTType.png.identifier as CFString,
          1,
          nil
      ) else {
    FileHandle.standardError.write(Data("cannot create icon output\n".utf8))
    exit(1)
}
CGImageDestinationAddImage(destination, image, nil)
guard CGImageDestinationFinalize(destination) else {
    FileHandle.standardError.write(Data("cannot write icon output\n".utf8))
    exit(1)
}
