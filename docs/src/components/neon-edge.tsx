import { useEffect, useRef } from "react";

const REACH = 220;

const POINTER_QUERY = "(hover: hover) and (pointer: fine)";
const MOTION_QUERY = "(prefers-reduced-motion: reduce)";

const hosts = new Map<HTMLElement, DOMRect | null>();
const onScreen = new Set<HTMLElement>();

let listening = false;
let frame = 0;
let cursor: { x: number; y: number } | null = null;
let visibility: IntersectionObserver | null = null;
let resize: ResizeObserver | null = null;

function paint() {
  frame = 0;

  for (const host of onScreen) {
    if (!cursor) {
      host.style.setProperty("--neon-a", "0");
      continue;
    }

    let box = hosts.get(host);
    if (!box) {
      box = host.getBoundingClientRect();
      hosts.set(host, box);
    }

    const x = cursor.x - box.left;
    const y = cursor.y - box.top;

    const gapX = Math.max(box.left - cursor.x, 0, cursor.x - box.right);
    const gapY = Math.max(box.top - cursor.y, 0, cursor.y - box.bottom);
    const lit = Math.hypot(gapX, gapY) < REACH;

    host.style.setProperty("--neon-t", `translate3d(${x}px, ${y}px, 0)`);
    host.style.setProperty("--neon-a", lit ? "1" : "0");
  }
}

function schedule() {
  if (frame !== 0) return;
  frame = requestAnimationFrame(paint);
}

function onMove(event: PointerEvent) {
  cursor = { x: event.clientX, y: event.clientY };
  schedule();
}

function onLeave() {
  cursor = null;
  schedule();
}

function invalidate() {
  for (const host of hosts.keys()) hosts.set(host, null);
  schedule();
}

function listen() {
  if (listening) return;
  listening = true;

  visibility = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const host = entry.target as HTMLElement;
        if (entry.isIntersecting) {
          onScreen.add(host);
        } else {
          onScreen.delete(host);
          host.style.setProperty("--neon-a", "0");
        }
      }
      schedule();
    },
    { threshold: 0 },
  );
  for (const host of hosts.keys()) visibility.observe(host);

  resize = new ResizeObserver(invalidate);
  resize.observe(document.documentElement);

  window.addEventListener("pointermove", onMove, { passive: true });
  window.addEventListener("scroll", invalidate, { passive: true });
  document.addEventListener("pointerleave", onLeave);
}

function unlisten() {
  if (!listening) return;
  listening = false;

  if (frame !== 0) {
    cancelAnimationFrame(frame);
    frame = 0;
  }
  cursor = null;
  onScreen.clear();

  visibility?.disconnect();
  visibility = null;
  resize?.disconnect();
  resize = null;

  window.removeEventListener("pointermove", onMove);
  window.removeEventListener("scroll", invalidate);
  document.removeEventListener("pointerleave", onLeave);
}

function register(host: HTMLElement) {
  hosts.set(host, null);
  if (listening) visibility?.observe(host);
  else listen();

  return () => {
    hosts.delete(host);
    onScreen.delete(host);
    visibility?.unobserve(host);
    if (hosts.size === 0) unlisten();
  };
}

export function useNeonEdge<T extends HTMLElement>() {
  const ref = useRef<T>(null);

  useEffect(() => {
    const host = ref.current;
    if (!host) return;

    const pointer = window.matchMedia(POINTER_QUERY);
    const motion = window.matchMedia(MOTION_QUERY);

    let teardown = () => {};
    const sync = () => {
      teardown();
      teardown = pointer.matches && !motion.matches ? register(host) : () => {};
    };

    sync();
    pointer.addEventListener("change", sync);
    motion.addEventListener("change", sync);

    return () => {
      pointer.removeEventListener("change", sync);
      motion.removeEventListener("change", sync);
      teardown();
    };
  }, []);

  return ref;
}

export function NeonEdge() {
  return (
    <>
      <span aria-hidden className="neon-wash">
        <span className="neon-glow" />
      </span>
      <span aria-hidden className="neon-edge">
        <span className="neon-glow" />
      </span>
    </>
  );
}
