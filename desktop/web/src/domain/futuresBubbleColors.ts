type Lab = readonly [number, number, number];
type Candidate = { color: string; lab: Lab; distance: number };

function lab(red: number, green: number, blue: number): Lab {
  const linear = (channel: number) => {
    const value = channel / 255;
    return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
  };
  const [r, g, b] = [red, green, blue].map(linear);
  const transform = (value: number) => value > 216 / 24389 ? Math.cbrt(value) : (24389 / 27 * value + 16) / 116;
  const x = transform((.4124564 * r + .3575761 * g + .1804375 * b) / .95047);
  const y = transform(.2126729 * r + .7151522 * g + .072175 * b);
  const z = transform((.0193339 * r + .119192 * g + .9503041 * b) / 1.08883);
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
}

function distanceSquared(first: Lab, second: Lab): number {
  return first.reduce((sum, value, index) => sum + (value - second[index]) ** 2, 0);
}

function hue(red: number, green: number, blue: number): number {
  const maximum = Math.max(red, green, blue), minimum = Math.min(red, green, blue);
  const span = maximum - minimum;
  if (!span) return 0;
  const sector = maximum === red ? (green - blue) / span : maximum === green ? (blue - red) / span + 2 : (red - green) / span + 4;
  return ((sector * 60) % 360 + 360) % 360;
}

function buildPalette(): Candidate[] {
  const candidates: Candidate[] = [];
  for (let red = 0; red <= 255; red += 6) for (let green = 0; green <= 255; green += 6) for (let blue = 0; blue <= 255; blue += 6) {
    const angle = hue(red, green, blue), value = lab(red, green, blue);
    // Avoid red/green families, including olive and yellow-green shades.
    if (!((angle >= 35 && angle <= 70 && value[1] >= 0) || (angle >= 190 && angle <= 320))) continue;
    if (value[0] < 58 || value[0] > 96 || Math.hypot(value[1], value[2]) < 12) continue;
    candidates.push({ color: `#${[red, green, blue].map(channel => channel.toString(16).padStart(2, "0")).join("")}`, lab: value, distance: Infinity });
  }
  const palette: Candidate[] = [];
  let next = candidates.find(candidate => candidate.color === "#3344cc") || candidates[0];
  while (next && palette.length < 256) {
    palette.push(next);
    for (const candidate of candidates) candidate.distance = Math.min(candidate.distance, distanceSquared(candidate.lab, next.lab));
    next = candidates.reduce((farthest, candidate) => candidate.distance > farthest.distance ? candidate : farthest, candidates[0]);
    if (next.distance <= 10.01 ** 2) break;
  }
  return palette;
}

const palette = buildPalette().slice(0, 16);
const assignments = new Map<string, string>();

export function bubbleColorsFor(keys: readonly string[]): Record<string, string> {
  for (const key of [...new Set(keys)].sort()) {
    if (assignments.has(key)) continue;
    assignments.set(key, palette[assignments.size % palette.length].color);
  }
  return Object.fromEntries(keys.map(key => [key, assignments.get(key)!]));
}

export function minimumBubbleColorDistance(colors: readonly string[]): number {
  const values = [...new Set(colors)].map(color => lab(...[1, 3, 5].map(offset => Number.parseInt(color.slice(offset, offset + 2), 16)) as [number, number, number]));
  let minimum = Infinity;
  for (let first = 0; first < values.length; first++) for (let second = first + 1; second < values.length; second++)
    minimum = Math.min(minimum, distanceSquared(values[first], values[second]));
  return Math.sqrt(minimum);
}

export const bubbleColorCapacity = palette.length;

export function minimumBubbleTextContrast(colors: readonly string[]): number {
  return Math.min(...colors.map(color => {
    const [lightness] = lab(...[1, 3, 5].map(offset => Number.parseInt(color.slice(offset, offset + 2), 16)) as [number, number, number]);
    const luminance = lightness > 8 ? ((lightness + 16) / 116) ** 3 : lightness / (24389 / 27);
    return (luminance + .05) / .05;
  }));
}
