import type { Status } from '../api';
import { setupName } from '../api';

export function LogoMark({ size = 34 }: { size?: number }) {
  // A parallel gripper: wrist bar, two jaws, and the cube it holds.
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="10" fill="var(--mint)" />
      <g fill="none" stroke="var(--mint-ink)" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 6h8" />
        <path d="M13.5 6v4L9 16v9" />
        <path d="M18.5 6v4l4.5 6v9" />
      </g>
      <rect x="13" y="18.5" width="6" height="6" rx="1.2" fill="var(--mint-ink)" />
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
    </div>
  );
}
