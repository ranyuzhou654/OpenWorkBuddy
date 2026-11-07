import { useEffect, useRef } from "react";

type Dust = {
  x: number; y: number; vx: number; vy: number;
  r: number; base: number; angle: number; phase: number; depth: number; alpha: number; tone: boolean;
};

type Ripple = { x: number; y: number; at: number };
type Rgb = [number, number, number];

const TAU = Math.PI * 2;
const clamp = (value: number) => Math.max(0, Math.min(1, value));
const smooth = (from: number, to: number, value: number) => {
  const t = clamp((value - from) / (to - from));
  return t * t * (3 - 2 * t);
};

function rgb(value: string, fallback: Rgb): Rgb {
  const hex = value.trim().match(/^#([0-9a-f]{6})$/i);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const parts = value.match(/\d+/g);
  return parts && parts.length >= 3 ? [+parts[0], +parts[1], +parts[2]] : fallback;
}

const fill = ([r, g, b]: Rgb) => `rgb(${r},${g},${b})`;

export function HomeAmbient() {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    let width = 0;
    let height = 0;
    let dust: Dust[] = [];
    let ripple: Ripple | null = null;
    let dustFill = "rgb(117,114,106)";
    let accentFill = "rgb(201,100,66)";
    let intensity = 1;
    let frame = 0;
    let last = 0;
    let visible = document.visibilityState !== "hidden";
    let disposed = false;

    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let reduced = motion.matches;
    const pointer = { x: -9999, y: -9999, active: false };

    const readColors = () => {
      const style = getComputedStyle(document.documentElement);
      dustFill = fill(rgb(style.getPropertyValue("--muted"), [117, 114, 106]));
      accentFill = fill(rgb(style.getPropertyValue("--clay"), [201, 100, 66]));
    };

    const mask = (x: number, y: number) => {
      const rx = Math.min(380, Math.max(210, width * 0.34));
      const ry = Math.min(240, Math.max(145, height * 0.2));
      const nx = (x - width / 2) / rx;
      const ny = (y - height * 0.46) / ry;
      return 0.18 + 0.82 * smooth(0.55, 1.35, Math.hypot(nx, ny));
    };

    const build = () => {
      const count = Math.max(34, Math.min(96, Math.round((width * height) / 13000)));
      dust = Array.from({ length: count }, () => {
        const depth = 0.35 + Math.random() * 0.65;
        return {
          x: Math.random() * width,
          y: Math.random() * height,
          vx: 0,
          vy: 0,
          r: 0.7 + depth * 1.4,
          base: 2.5 + depth * 4.5,
          angle: Math.random() * TAU,
          phase: Math.random() * TAU,
          depth,
          alpha: 0.17 + depth * 0.21,
          tone: Math.random() < 0.12,
        };
      });
    };

    const paint = () => {
      ctx.clearRect(0, 0, width, height);
      for (const p of dust) {
        let a = p.alpha * intensity * mask(p.x, p.y) * (0.75 + p.depth * 0.25);
        if (pointer.active) {
          const distance = Math.hypot(p.x - pointer.x, p.y - pointer.y);
          if (distance < 170) a += (1 - distance / 170) * 0.1 * intensity;
        }
        ctx.fillStyle = p.tone ? accentFill : dustFill;
        ctx.globalAlpha = Math.min(0.5, a * 0.34);
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r * 2.4, 0, TAU);
        ctx.fill();
        ctx.globalAlpha = Math.min(0.62, a * 1.08);
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, TAU);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    };

    const paintRipple = (now: number) => {
      if (!ripple) return;
      const age = (now - ripple.at) / 1000;
      if (age > 0.72) { ripple = null; return; }
      const k = clamp(age / 0.72);
      const radius = 6 + 96 * (1 - Math.pow(1 - k, 2));
      ctx.strokeStyle = accentFill;
      ctx.lineWidth = 1.2;
      ctx.globalAlpha = (1 - k) * 0.3 * intensity;
      ctx.beginPath();
      ctx.arc(ripple.x, ripple.y, radius, 0, TAU);
      ctx.stroke();
      ctx.globalAlpha = (1 - k) * 0.12 * intensity;
      ctx.beginPath();
      ctx.arc(ripple.x, ripple.y, radius * 0.55, 0, TAU);
      ctx.stroke();
      ctx.globalAlpha = 1;
    };

    const renderStill = () => {
      paint();
      ctx.globalAlpha = 1;
    };

    const tick = (now: number) => {
      frame = 0;
      if (!visible || disposed) return;
      if (!last) last = now - 16;
      const dt = Math.min(0.034, Math.max(0.001, (now - last) / 1000));
      last = now;

      const active = document.activeElement as HTMLElement | null;
      const typing = Boolean(active && canvas.parentElement?.contains(active)
        && (active.tagName === "TEXTAREA" || active.tagName === "INPUT"));
      intensity += ((typing ? 0.36 : 1) - intensity) * Math.min(1, dt * 3);

      const time = now / 1000;
      for (const p of dust) {
        const ease = 1 - Math.exp(-dt * 0.7);
        p.vx += (Math.cos(p.angle) * p.base - p.vx) * ease;
        p.vy += (Math.sin(p.angle) * p.base - p.vy) * ease;
        p.vx += Math.cos(time * 0.55 + p.phase) * 3 * dt * p.depth;
        p.vy += Math.sin(time * 0.48 + p.phase * 1.3) * 3 * dt * p.depth;

        if (pointer.active) {
          const dx = p.x - pointer.x;
          const dy = p.y - pointer.y;
          const distance = Math.hypot(dx, dy);
          if (distance > 0.1 && distance < 170) {
            const falloff = 1 - distance / 170;
            const push = falloff * falloff * 780 * p.depth * dt;
            p.vx += (dx / distance) * push;
            p.vy += (dy / distance) * push;
            const swirl = falloff * 120 * p.depth * dt;
            p.vx += (-dy / distance) * swirl;
            p.vy += (dx / distance) * swirl;
          }
        }

        const speed = Math.hypot(p.vx, p.vy);
        if (speed > 90) { p.vx = (p.vx / speed) * 90; p.vy = (p.vy / speed) * 90; }
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        if (p.x < -12) p.x = width + 12;
        else if (p.x > width + 12) p.x = -12;
        if (p.y < -12) p.y = height + 12;
        else if (p.y > height + 12) p.y = -12;
      }

      paint();
      paintRipple(now);
      frame = requestAnimationFrame(tick);
    };

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) return;
      const nextWidth = rect.width;
      const nextHeight = rect.height;
      if (Math.abs(nextWidth - width) < 1 && Math.abs(nextHeight - height) < 1) return;
      width = nextWidth;
      height = nextHeight;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      readColors();
      build();
      if (reduced) renderStill();
    };

    const isInteractive = (target: EventTarget | null) => {
      if (!(target instanceof Element)) return false;
      return Boolean(target.closest(
        "button, a, input, textarea, select, label, [role='button'], [role='combobox'], [role='dialog'], .composer, .home-intro",
      ));
    };

    const onMove = (event: PointerEvent) => {
      if (reduced) return;
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      pointer.active = x >= 0 && y >= 0 && x <= rect.width && y <= rect.height;
      pointer.x = x;
      pointer.y = y;
    };

    const onLeave = () => {
      pointer.active = false;
      pointer.x = -9999;
      pointer.y = -9999;
    };

    const onDown = (event: PointerEvent) => {
      if (reduced || event.button !== 0 || isInteractive(event.target)) return;
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      if (x < 0 || y < 0 || x > rect.width || y > rect.height) return;
      ripple = { x, y, at: performance.now() };
      for (const p of dust) {
        const dx = p.x - x;
        const dy = p.y - y;
        const distance = Math.hypot(dx, dy);
        if (distance > 0.1 && distance < 130) {
          const push = (1 - distance / 130) * 46 * p.depth;
          p.vx += (dx / distance) * push;
          p.vy += (dy / distance) * push;
        }
      }
      if (!frame && !reduced) frame = requestAnimationFrame(tick);
    };

    const start = () => {
      if (reduced || !visible || disposed) return;
      last = 0;
      if (!frame) frame = requestAnimationFrame(tick);
    };

    const onVisibility = () => {
      visible = document.visibilityState !== "hidden";
      if (!visible) {
        cancelAnimationFrame(frame);
        frame = 0;
      } else if (reduced) renderStill();
      else start();
    };

    const onMotion = () => {
      reduced = motion.matches;
      cancelAnimationFrame(frame);
      frame = 0;
      if (reduced) renderStill();
      else start();
    };

    const onTheme = () => {
      readColors();
      if (reduced) renderStill();
    };

    readColors();
    resize();
    const observer = new ResizeObserver(resize);
    if (canvas.parentElement) observer.observe(canvas.parentElement);
    const themeObserver = new MutationObserver(onTheme);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("pointerleave", onLeave);
    window.addEventListener("pointerdown", onDown, true);
    motion.addEventListener("change", onMotion);
    scheme.addEventListener("change", onTheme);

    if (reduced) renderStill();
    else start();

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      themeObserver.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("pointerdown", onDown, true);
      motion.removeEventListener("change", onMotion);
      scheme.removeEventListener("change", onTheme);
    };
  }, []);

  return <canvas ref={ref} className="home-ambient" aria-hidden="true" />;
}
