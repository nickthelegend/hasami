import type { Status } from '../api';
import { setupName } from '../api';

export function LogoMark({ size = 26 }: { size?: number }) {
  // Hasami (鋏) means scissors: two finger loops, two blades crossing.
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" className="logo">
      <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <circle cx="6" cy="6" r="3.2" />
        <circle cx="6" cy="18" r="3.2" />
        <path d="M8.7 7.8 21 16.5M8.7 16.2 21 7.5" />
      </g>
    </svg>
  );
}

type PillState = 'ok' | 'setup' | 'off' | 'wait';

function Pill({ label, value, state, title }: { label: string; value: string; state: PillState; title?: string }) {
  return (
    <span className={`spill spill-${state}`} title={title}>
      <i aria-hidden="true" />
      <b>{label}</b>
      <span>{value}</span>
    </span>
  );
}

/** "sam2.1-hiera-tiny (local)" → "SAM 2.1"; "sam-3.1 (Meta Model API)" → "SAM 3.1" */
const samLabel = (name: string) => {
  const m = name.match(/sam[\s-]?(\d+(?:\.\d+)?)/i);
  return m ? `SAM ${m[1]}` : 'SAM';
};
const samWhere = (name: string) => (/local/i.test(name) ? 'local' : /meta|api/i.test(name) ? 'Meta API' : 'ready');

export function StatusPills({ status, reachable }: { status: Status | null; reachable: boolean | null }) {
  if (reachable === null) {
    return (
      <div className="pills" aria-live="polite">
        <Pill label="Server" value="connecting…" state="wait" />
      </div>
    );
  }
  if (!reachable || !status) {
    return (
      <div className="pills" aria-live="polite">
        <Pill label="Server" value="not reachable" state="off" title="python -m hasami_server" />
      </div>
    );
  }
  const p = status.planner;
  const arm =
    status.arm === 'so101' && status.so101.connected
      ? { value: `SO-101 · ${status.so101.port ?? 'connected'}`, state: 'ok' as const, title: 'Real SO-101 follower arm via LeRobot' }
      : status.arm === 'so101'
        ? { value: `Setup required: ${setupName(status.so101.setup ?? 'HASAMI_SO101_PORT')}`, state: 'setup' as const, title: status.so101.setup }
        : { value: 'SO-101 simulation', state: 'ok' as const, title: 'Official SO-101 MuJoCo model, force-limited servos' };
  return (
    <div className="pills" aria-label="System status">
      <Pill
        label="Planner"
        value={p.ready ? p.model : `Setup required: ${setupName(p.setup ?? 'MODEL_API_KEY')}`}
        state={p.ready ? 'ok' : 'setup'}
        title={p.ready ? `Muse planner ${p.model}` : p.setup}
      />
      {status.segmenters.length === 0 ? (
        <Pill label="SAM" value="none loaded" state="off" />
      ) : (
        status.segmenters.map((s) => (
          <Pill
            key={s.name}
            label={samLabel(s.model ?? s.name)}
            value={s.ready ? samWhere(s.name) : `Setup required: ${setupName(s.setup)}`}
            state={s.ready ? 'ok' : 'setup'}
            title={s.ready ? [s.model ?? s.name, s.prompt && `${s.prompt} prompt`, s.note].filter(Boolean).join(' · ') : `${s.model ?? s.name}: ${s.setup ?? 'not ready'}`}
          />
        ))
      )}
      <Pill label="Arm" value={arm.value} state={arm.state} title={arm.title} />
      {status.arm === 'sim' && (
        <Pill
          label="Real arm"
          value={`Setup required: ${setupName(status.so101.setup ?? 'HASAMI_SO101_PORT')}`}
          state="setup"
          title={status.so101.setup ?? 'LeRobot SO-101 follower on HASAMI_SO101_PORT'}
        />
      )}
    </div>
  );
}
