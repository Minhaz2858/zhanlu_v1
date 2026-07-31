export default function TopologyDiagram({ topology }) {
  const boxes = [
    { x: 16, y: 3, label: 'A' },
    { x: 16, y: 27, label: 'B' },
    { x: 16, y: 51, label: 'C' },
  ];
  const BW = 28, BH = 11;

  const DownArrow = ({ y }) => (
    <g stroke="currentColor" strokeWidth={1} fill="currentColor">
      <line x1={30} y1={y} x2={30} y2={y + 6} />
      <polygon points={`28,${y + 6} 32,${y + 6} 30,${y + 9}`} stroke="none" />
    </g>
  );

  return (
    <svg viewBox="0 0 52 66" width="52" height="66" className="mx-auto block">
      <g className="text-muted-foreground">
        {topology === 'sequence' && (
          <>
            <DownArrow y={14} />
            <DownArrow y={38} />
          </>
        )}
        {topology === 'loop' && (
          <>
            <DownArrow y={14} />
            <DownArrow y={38} />
            <path d="M 4.5 8.5 L 4.5 56.5 L 16 56.5" fill="none" stroke="currentColor" strokeWidth={1} />
            <line x1={4.5} y1={8.5} x2={12} y2={8.5} stroke="currentColor" strokeWidth={1} />
            <polygon points="12,6 12,11 15.5,8.5" fill="currentColor" stroke="none" />
          </>
        )}
        {topology === 'parallel' && (
          <>
            <line x1={8.5} y1={8.5} x2={8.5} y2={56.5} stroke="currentColor" strokeWidth={1} />
            <line x1={8.5} y1={8.5} x2={16} y2={8.5} stroke="currentColor" strokeWidth={1} />
            <line x1={8.5} y1={32.5} x2={16} y2={32.5} stroke="currentColor" strokeWidth={1} />
            <line x1={8.5} y1={56.5} x2={16} y2={56.5} stroke="currentColor" strokeWidth={1} />
          </>
        )}
      </g>
      <g className="text-primary">
        {boxes.map((b, i) => (
          <g key={i}>
            <rect x={b.x} y={b.y} width={BW} height={BH} rx={2} fill="currentColor" fillOpacity={0.12} stroke="currentColor" strokeWidth={1} />
            <text x={b.x + BW / 2} y={b.y + BH / 2} textAnchor="middle" dominantBaseline="central" fontSize={7} fill="currentColor" className="font-medium">{b.label}</text>
          </g>
        ))}
      </g>
    </svg>
  );
}