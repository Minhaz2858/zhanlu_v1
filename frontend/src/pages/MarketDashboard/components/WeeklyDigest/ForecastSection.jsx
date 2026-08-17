import { TYPO } from '../../lib/typography';

const SCENARIO_COLORS = {
  base: { bg: 'bg-blue-50', text: 'text-blue-700', border: 'border-blue-200' },
  bull: { bg: 'bg-green-50', text: 'text-green-700', border: 'border-green-200' },
  bear: { bg: 'bg-red-50', text: 'text-red-700', border: 'border-red-200' },
};

function ConfidenceBadge({ confidence }) {
  if (confidence == null) return null;
  const pct = Math.round(confidence * 100);
  const color = pct >= 70 ? 'bg-green-100 text-green-700' : pct >= 40 ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700';
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${color}`}>
      {pct}% conf.
    </span>
  );
}

function ScenarioCard({ label, value, color, price }) {
  return (
    <div className={`rounded-md border px-3 py-2 ${color.bg} ${color.border}`}>
      <div className="flex items-center justify-between">
        <span className={`text-xs font-medium ${color.text}`}>{label}</span>
        {value != null && (
          <span className={`text-sm font-bold font-mono ${color.text}`}>
            {Number(value).toLocaleString()}
          </span>
        )}
      </div>
      {price != null && value != null && (
        <div className={`text-[10px] mt-0.5 ${color.text} opacity-70`}>
          {((Number(value) - price) / price * 100).toFixed(1)}% vs current
        </div>
      )}
    </div>
  );
}

export default function ForecastSection({ forecast7d, forecast30d, currentPrice }) {
  const hasData = forecast7d || forecast30d;

  if (!hasData) {
    return (
      <div className="text-xs text-slate-400 py-2">No forecast data available</div>
    );
  }

  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-slate-600 flex items-center gap-2">
        预测 Forecasting
      </h4>

      {forecast7d && (
        <div>
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-xs font-medium text-slate-700">7-Day</span>
            <ConfidenceBadge confidence={forecast7d.confidence} />
          </div>
          <div className="grid grid-cols-3 gap-2">
            <ScenarioCard label="基准 Base" value={forecast7d.base} color={SCENARIO_COLORS.base} price={currentPrice} />
            <ScenarioCard label="看涨 Bull" value={forecast7d.bull} color={SCENARIO_COLORS.bull} price={currentPrice} />
            <ScenarioCard label="看跌 Bear" value={forecast7d.bear} color={SCENARIO_COLORS.bear} price={currentPrice} />
          </div>
        </div>
      )}

      {forecast30d && (
        <div>
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-xs font-medium text-slate-700">30-Day</span>
            <ConfidenceBadge confidence={forecast30d.confidence} />
          </div>
          <div className="grid grid-cols-3 gap-2">
            <ScenarioCard label="基准 Base" value={forecast30d.base} color={SCENARIO_COLORS.base} price={currentPrice} />
            <ScenarioCard label="看涨 Bull" value={forecast30d.bull} color={SCENARIO_COLORS.bull} price={currentPrice} />
            <ScenarioCard label="看跌 Bear" value={forecast30d.bear} color={SCENARIO_COLORS.bear} price={currentPrice} />
          </div>
        </div>
      )}
    </div>
  );
}
