// Explicit offline rendering exercise required by D025; never a server fallback.
import React from '../packages/dashboard/node_modules/react';
import { createRoot } from '../packages/dashboard/node_modules/react-dom/client';
import { InstructionCard, type PlanView } from '../packages/dashboard/src/components/Panels';
import '../packages/dashboard/src/styles.css';
const plan: PlanView = new URLSearchParams(location.search).get('view') === 'empty'
  ? { state: 'ok', res: { plan: [], model: 'offline component exercise', latency_ms: 0 } }
  : { state: 'error', message: 'Local planning error for offline component audit.' };
createRoot(document.getElementById('root')!).render(<InstructionCard value="Offline component audit" onChange={() => {}} onPlan={() => {}} status={null} disabled={true} plan={plan} />);
