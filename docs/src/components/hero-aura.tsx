import { useEffect, useRef } from "react";

type Bloom = {
  cx: number;
  cy: number;
  ax: number;
  ay: number;
  periodX: number;
  periodY: number;
  phaseX: number;
  phaseY: number;
  size: number;
  minSize: number;
  maxSize: number;
  push: number;
  color: string;
};

const BLOOMS: Bloom[] = [
  {
    cx: 0.22,
    cy: 0.34,
    ax: 0.13,
    ay: 0.16,
    periodX: 29,
    periodY: 37,
    phaseX: 0,
    phaseY: 1.1,
    size: 0.42,
    minSize: 230,
    maxSize: 560,
    push: 150,
    color: "rgba(34,211,238,0.16)",
  },
  {
    cx: 0.78,
    cy: 0.28,
    ax: 0.11,
    ay: 0.14,
    periodX: 23,
    periodY: 31,
    phaseX: 2.4,
    phaseY: 0.3,
    size: 0.34,
    minSize: 200,
    maxSize: 460,
    push: 120,
    color: "rgba(138,43,226,0.18)",
  },
  {
    cx: 0.54,
    cy: 0.74,
    ax: 0.16,
    ay: 0.1,
    periodX: 41,
    periodY: 26,
    phaseX: 4.2,
    phaseY: 3.0,
    size: 0.3,
    minSize: 180,
    maxSize: 420,
    push: 95,
    color: "rgba(255,47,208,0.09)",
  },
];

const PUSH_RADIUS = 260;
const PUSH_EASE = 0.08;
const MAX_DELTA = 0.05;

const MOTION_QUERY = "(prefers-reduced-motion: reduce)";
const POINTER_QUERY = "(hover: hover) and (pointer: fine)";

const TAU = Math.PI * 2;

export function HeroAura() {
  const host = useRef<HTMLDivElement>(null);
  const nodes = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    const container = host.current;
    if (!container) return;

    const layers = nodes.current.filter((n): n is HTMLDivElement => n !== null);
    if (layers.length === 0) return;

    const motion = window.matchMedia(MOTION_QUERY);
    const pointer = window.matchMedia(POINTER_QUERY);

    let teardown = () => {};

    const start = () => {
      let box = container.getBoundingClientRect();
      let boxStale = false;
      const measure = () => {
        box = container.getBoundingClientRect();
        boxStale = false;
      };
      const invalidate = () => {
        boxStale = true;
      };

      const sizes = BLOOMS.map((b) =>
        Math.round(Math.min(b.maxSize, Math.max(b.minSize, box.width * b.size))),
      );

      const applySizes = () => {
        layers.forEach((layer, i) => {
          const b = BLOOMS[i];
          sizes[i] = Math.round(
            Math.min(b.maxSize, Math.max(b.minSize, box.width * b.size)),
          );
          layer.style.width = `${sizes[i]}px`;
          layer.style.height = `${sizes[i]}px`;
        });
      };

      const driftAt = (b: Bloom, t: number) => ({
        x: box.width * (b.cx + b.ax * Math.sin((TAU * t) / b.periodX + b.phaseX)),
        y: box.height * (b.cy + b.ay * Math.sin((TAU * t) / b.periodY + b.phaseY)),
      });

      let elapsed = 0;
      const push = BLOOMS.map(() => ({ x: 0, y: 0 }));
      let cursor: { x: number; y: number } | null = null;

      const paint = () => {
        layers.forEach((layer, i) => {
          const drift = driftAt(BLOOMS[i], elapsed);
          const half = sizes[i] / 2;
          layer.style.transform = `translate3d(${
            drift.x + push[i].x - half
          }px, ${drift.y + push[i].y - half}px, 0)`;
        });
      };

      applySizes();
      paint();

      const onResize = () => {
        measure();
        applySizes();
        paint();
      };
      const resizeObserver = new ResizeObserver(onResize);
      resizeObserver.observe(container);

      if (motion.matches) {
        return () => resizeObserver.disconnect();
      }

      let frame = 0;
      let last = 0;
      let onScreen = true;

      const step = (now: number) => {
        const delta = last === 0 ? 0 : Math.min(MAX_DELTA, (now - last) / 1000);
        last = now;
        elapsed += delta;

        if (boxStale) measure();

        BLOOMS.forEach((b, i) => {
          let targetX = 0;
          let targetY = 0;

          if (cursor) {
            const drift = driftAt(b, elapsed);
            const dx = drift.x - cursor.x;
            const dy = drift.y - cursor.y;
            const dist = Math.hypot(dx, dy);

            if (dist > 0 && dist < PUSH_RADIUS) {
              const falloff = (1 - dist / PUSH_RADIUS) ** 2;
              targetX = (dx / dist) * falloff * b.push;
              targetY = (dy / dist) * falloff * b.push;
            }
          }

          push[i].x += (targetX - push[i].x) * PUSH_EASE;
          push[i].y += (targetY - push[i].y) * PUSH_EASE;
        });

        paint();
        frame = requestAnimationFrame(step);
      };

      const run = () => {
        if (frame !== 0 || !onScreen || document.hidden) return;
        last = 0;
        frame = requestAnimationFrame(step);
      };

      const halt = () => {
        if (frame === 0) return;
        cancelAnimationFrame(frame);
        frame = 0;
      };

      const visibility = new IntersectionObserver(
        ([entry]) => {
          onScreen = entry.isIntersecting;
          if (onScreen) run();
          else halt();
        },
        { threshold: 0 },
      );
      visibility.observe(container);

      const onVisibility = () => (document.hidden ? halt() : run());
      document.addEventListener("visibilitychange", onVisibility);
      window.addEventListener("scroll", invalidate, { passive: true });

      const finePointer = pointer.matches;

      const onMove = (event: PointerEvent) => {
        if (boxStale) measure();
        const x = event.clientX - box.left;
        const y = event.clientY - box.top;
        const inReach =
          x > -PUSH_RADIUS &&
          y > -PUSH_RADIUS &&
          x < box.width + PUSH_RADIUS &&
          y < box.height + PUSH_RADIUS;
        cursor = inReach ? { x, y } : null;
      };
      const onLeave = () => {
        cursor = null;
      };

      if (finePointer) {
        window.addEventListener("pointermove", onMove, { passive: true });
        document.addEventListener("pointerleave", onLeave);
      }

      run();

      return () => {
        halt();
        resizeObserver.disconnect();
        visibility.disconnect();
        document.removeEventListener("visibilitychange", onVisibility);
        window.removeEventListener("scroll", invalidate);
        if (finePointer) {
          window.removeEventListener("pointermove", onMove);
          document.removeEventListener("pointerleave", onLeave);
        }
      };
    };

    const restart = () => {
      teardown();
      teardown = start();
    };

    restart();
    motion.addEventListener("change", restart);
    pointer.addEventListener("change", restart);

    return () => {
      motion.removeEventListener("change", restart);
      pointer.removeEventListener("change", restart);
      teardown();
    };
  }, []);

  return (
    <div
      ref={host}
      aria-hidden
      className="pointer-events-none absolute inset-0 z-0 overflow-hidden"
    >
      {BLOOMS.map((bloom, i) => (
        <div
          key={i}
          ref={(node) => {
            nodes.current[i] = node;
          }}
          className="absolute left-0 top-0 rounded-full [filter:blur(90px)] [will-change:transform]"
          style={{ backgroundColor: bloom.color }}
        />
      ))}
    </div>
  );
}
