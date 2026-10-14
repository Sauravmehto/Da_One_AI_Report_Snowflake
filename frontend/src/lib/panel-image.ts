/**
 * Turning a rendered part of the page into a PNG the PDF export can embed.
 *
 * A chart is more than its SVG: the axis titles above it, its legend, a
 * bubble chart's size key and a heatmap's grid are HTML. So the element is
 * drawn whole, as HTML inside an SVG <foreignObject>, onto a canvas.
 *
 * WHY THE STYLES ARE INLINED
 * --------------------------
 * An SVG drawn as an image has no access to the page's stylesheets. Classes
 * and CSS custom properties (`var(--muted-foreground)`) resolve to nothing, so
 * every element's COMPUTED style is copied onto the clone first. Only what
 * differs from the browser's default for that element is written, or a chart
 * of a few thousand nodes serialises to tens of megabytes.
 *
 * WHY THE FONT IS EMBEDDED
 * ------------------------
 * The same isolation hides the page's web font, and text drawn in a fallback
 * face is wider than the layout it was measured for: labels overran the gaps
 * made for them. The font file is read once and inlined as a data URL.
 */

const SVG_NS = "http://www.w3.org/2000/svg"

/** Rendered at twice its size, so the PDF is not a blurry upscale. */
const SCALE = 2

/** Properties a child takes from its parent unless it sets its own. */
const INHERITED = [
  "color",
  "cursor",
  "direction",
  "dominant-baseline",
  "fill",
  "fill-opacity",
  "font-family",
  "font-feature-settings",
  "font-size",
  "font-style",
  "font-variant-numeric",
  "font-weight",
  "letter-spacing",
  "line-height",
  "paint-order",
  "stroke",
  "stroke-dasharray",
  "stroke-linecap",
  "stroke-linejoin",
  "stroke-opacity",
  "stroke-width",
  "text-align",
  "text-anchor",
  "text-transform",
  "visibility",
  "white-space",
  "word-spacing",
] as const
const INHERITED_SET = new Set<string>(INHERITED)

/**
 * Written on every HTML element whatever their value. A border's width only
 * computes to 0 because its style is none; written without the width, a style
 * of solid (Tailwind sets one on everything) brought back the default medium
 * border, and every box in the picture grew a grey frame.
 */
const ALWAYS = [
  "box-sizing",
  "border-top-style", "border-right-style", "border-bottom-style", "border-left-style",
  "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
  "outline-style", "outline-width",
] as const
const ALWAYS_SET = new Set<string>(ALWAYS)

/** What an SVG shape needs beyond its attributes: paint, text and visibility. */
const SVG_PROPERTIES = [...INHERITED, "display", "opacity", "transform", "transform-origin"] as const

let defaultsFrame: HTMLIFrameElement | null = null
const defaults = new Map<string, Map<string, string>>()

/** The browser's own style for an element, read in an unstyled document. */
function defaultStyle(element: Element): Map<string, string> {
  return defaultStyleFor(element.namespaceURI === SVG_NS, element.localName)
}

function defaultStyleFor(svg: boolean, localName: string): Map<string, string> {
  const key = `${svg ? "svg:" : ""}${localName}`
  const cached = defaults.get(key)
  if (cached) return cached

  if (!defaultsFrame) {
    defaultsFrame = document.createElement("iframe")
    defaultsFrame.setAttribute("aria-hidden", "true")
    defaultsFrame.style.cssText =
      "position:fixed;left:-10000px;top:0;width:10px;height:10px;border:0;"
    document.body.appendChild(defaultsFrame)
  }
  const doc = defaultsFrame.contentDocument
  const view = defaultsFrame.contentWindow
  const out = new Map<string, string>()
  if (!doc || !view) return out

  const probe = svg ? doc.createElementNS(SVG_NS, localName) : doc.createElement(localName)
  const holder = svg && localName !== "svg" ? doc.createElementNS(SVG_NS, "svg") : null
  if (holder) {
    holder.appendChild(probe)
    doc.body.appendChild(holder)
  } else {
    doc.body.appendChild(probe)
  }
  const computed = view.getComputedStyle(probe)
  for (let i = 0; i < computed.length; i++) {
    const name = computed[i]
    out.set(name, computed.getPropertyValue(name))
  }
  ;(holder ?? probe).remove()
  defaults.set(key, out)
  return out
}

/** Write the computed style of `source` onto `target`, as far as it matters. */
function inlineStyles(source: Element, target: Element, parent: CSSStyleDeclaration | null): void {
  const computed = window.getComputedStyle(source)
  const svgChild = source.namespaceURI === SVG_NS && source.localName !== "svg"
  const base = defaultStyle(source)
  // What an ordinary element of the same kind starts with. A button or an
  // input has a font of its own instead of its parent's, so matching the
  // parent is not enough to leave its font unwritten.
  const plain = defaultStyleFor(svgChild, svgChild ? "g" : "div")
  const parts: string[] = []

  const consider = (name: string) => {
    const value = computed.getPropertyValue(name)
    if (!value) return
    // An inherited property is written wherever it differs from what the
    // parent passes down, or the element does not inherit it by default; any
    // other, wherever it differs from the default.
    const inherited = INHERITED_SET.has(name)
    const reference = inherited && parent ? parent.getPropertyValue(name) : base.get(name)
    const ownDefault = inherited && base.get(name) !== plain.get(name)
    if (value !== reference || ownDefault || ALWAYS_SET.has(name) || (!parent && inherited)) {
      parts.push(`${name}:${value}`)
    }
  }

  if (svgChild) {
    for (const name of SVG_PROPERTIES) consider(name)
  } else {
    for (let i = 0; i < computed.length; i++) consider(computed[i])
  }
  if (parts.length) target.setAttribute("style", parts.join(";"))
  target.removeAttribute("class")

  const from = source.children
  const to = target.children
  for (let i = 0; i < from.length && i < to.length; i++) {
    inlineStyles(from[i], to[i], computed)
  }
}

let fontCss: Promise<string> | null = null

function readAsDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

function fontFaceRules(rules: CSSRuleList, out: CSSFontFaceRule[]): void {
  for (const rule of Array.from(rules)) {
    if (rule instanceof CSSFontFaceRule) out.push(rule)
    else if ("cssRules" in rule) fontFaceRules((rule as CSSGroupingRule).cssRules, out)
  }
}

/**
 * @font-face rules for the page's font, with the font files inlined.
 *
 * Latin, upright faces only: that is what the charts print, and every other
 * subset would add its file to every picture. Empty when the font cannot be
 * read; the picture then uses a fallback face rather than failing.
 */
export function embeddedFontCss(): Promise<string> {
  fontCss ??= (async () => {
    const family = window.getComputedStyle(document.body).fontFamily.split(",")[0].replace(/["']/g, "").trim()
    const rules: CSSFontFaceRule[] = []
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        fontFaceRules(sheet.cssRules, rules)
      } catch {
        // A stylesheet from another origin cannot be read; it holds no font of ours.
      }
    }
    const wanted = rules.filter((rule) => {
      const style = rule.style
      const name = style.getPropertyValue("font-family").replace(/["']/g, "").trim()
      const range = style.getPropertyValue("unicode-range")
      return (
        name === family &&
        style.getPropertyValue("font-style") !== "italic" &&
        (!range || /U\+0+-0*FF\b/i.test(range))
      )
    })
    const parts = await Promise.all(
      wanted.map(async (rule) => {
        let css = rule.cssText
        const base = rule.parentStyleSheet?.href ?? window.location.href
        for (const match of css.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/g)) {
          if (match[1].startsWith("data:")) continue
          const response = await fetch(new URL(match[1], base))
          const data = await readAsDataUrl(await response.blob())
          css = css.replace(match[0], `url("${data}")`)
        }
        return css
      })
    )
    return parts.join("\n")
  })().catch(() => "")
  return fontCss
}

/**
 * A PNG data URL of an element as it is drawn on the page, or null.
 *
 * Returns null rather than throwing: an export that loses one picture is worth
 * finishing, and the PDF draws that chart itself instead.
 */
export async function elementToPng(element: HTMLElement): Promise<string | null> {
  try {
    const box = element.getBoundingClientRect()
    const width = Math.max(Math.ceil(box.width), 1)
    const height = Math.max(Math.ceil(box.height), 1)

    const clone = element.cloneNode(true) as HTMLElement
    inlineStyles(element, clone, null)
    clone.style.setProperty("width", `${width}px`)
    clone.style.setProperty("height", `${height}px`)
    clone.style.setProperty("margin", "0")

    const font = await embeddedFontCss()
    const html = new XMLSerializer().serializeToString(clone)
    const svg =
      `<svg xmlns="${SVG_NS}" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">` +
      `<style>${font}</style>` +
      `<foreignObject x="0" y="0" width="${width}" height="${height}">${html}</foreignObject>` +
      `</svg>`

    const image = new Image()
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
    await image.decode()

    const canvas = document.createElement("canvas")
    canvas.width = width * SCALE
    canvas.height = height * SCALE
    const context = canvas.getContext("2d")
    if (!context) return null
    // The chart is drawn on transparency; the PDF page is white.
    context.fillStyle = "#ffffff"
    context.fillRect(0, 0, canvas.width, canvas.height)
    context.drawImage(image, 0, 0, canvas.width, canvas.height)
    return canvas.toDataURL("image/png")
  } catch {
    return null
  }
}
