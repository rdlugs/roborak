export function Logo({ size = "md" }: { size?: "sm" | "md" | "lg" }) {
  const type = { sm: "text-base", md: "text-lg", lg: "text-2xl" }[size];
  const box = { sm: "h-6 w-6", md: "h-7 w-7", lg: "h-10 w-10" }[size];
  const glyph = { sm: "text-[11px]", md: "text-xs", lg: "text-base" }[size];

  return (
    <span className="flex flex-row items-center gap-2.5">
      <span
        className={`${box} flex items-center justify-center rounded-md border border-cyan/50 [background-color:rgba(34,211,238,0.10)] [box-shadow:0_0_18px_rgba(34,211,238,0.35)]`}
      >
        <span className={`font-mono font-bold text-cyan ${glyph}`}>rk</span>
      </span>
      <span className={`font-mono font-bold tracking-tight text-ink ${type}`}>roborak</span>
    </span>
  );
}
