import './index.css';

import { context } from '@devvit/web/client';
import { StrictMode, useState } from 'react';
import type { FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { trpc } from './trpc';
import type { inferRouterOutputs } from '@trpc/server';
import type { AppRouter } from '../server/trpc';

type SubmitResult = inferRouterOutputs<AppRouter>['verify']['submit'];

type Requirement = {
  label: string;
  value: number;
  threshold: number;
  unit: string;
  met: boolean;
};

function buildRequirements(
  metrics: NonNullable<SubmitResult['metrics']>,
  thresholds: NonNullable<SubmitResult['thresholds']>
): Requirement[] {
  const rows: Array<[string, number, number, string]> = [
    ['Account age', metrics.accountAgeDays, thresholds.minAccountAgeDays, ' days'],
    ['Total karma', metrics.totalKarma, thresholds.minTotalKarma, ''],
    [
      'Subreddit activity',
      metrics.subredditActivityCount,
      thresholds.minSubredditActivityCount,
      ' posts/comments',
    ],
    ['Subreddit karma', metrics.subredditKarma, thresholds.minSubredditKarma, ''],
  ];
  return rows.map(([label, value, threshold, unit]) => ({
    label,
    value,
    threshold,
    unit,
    met: value >= threshold,
  }));
}

export const Splash = () => {
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<SubmitResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!code.trim()) return;
    setSubmitting(true);
    setResult(null);
    setError(null);
    try {
      const response = await trpc.verify.submit.mutate({ code: code.trim() });
      setResult(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong -- try again.');
    } finally {
      setSubmitting(false);
    }
  };

  const requirements =
    result?.metrics && result.thresholds ? buildRequirements(result.metrics, result.thresholds) : null;

  // Color story matches the Discord DMs: green passed, amber "submitted but
  // didn't meet requirements" (you'll still get a DM with details), red error.
  const resultTone = error ? 'error' : result?.passed ? 'pass' : result ? 'fail' : null;
  const toneStyles = {
    pass: 'border-green-500 bg-green-50 dark:bg-green-950/40',
    fail: 'border-amber-500 bg-amber-50 dark:bg-amber-950/40',
    error: 'border-red-500 bg-red-50 dark:bg-red-950/40',
  } as const;

  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center gap-4 bg-white p-4 dark:bg-gray-900">
      <div className="flex flex-col items-center gap-2 text-center">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Verify for Discord</h1>
        <p className="max-w-sm text-base text-gray-600 dark:text-gray-300">
          Hey {context.username ?? 'there'} — paste the code from your Discord DM below to link
          your accounts.
        </p>
      </div>
      <form onSubmit={submit} className="flex w-full max-w-xs flex-col items-center gap-2">
        <input
          className="w-72 rounded border border-gray-300 bg-white px-3 py-2 text-center font-mono text-sm text-gray-900 placeholder:text-gray-400 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="paste your code here"
          maxLength={300}
          autoCapitalize="characters"
          disabled={submitting}
        />
        <button
          type="submit"
          disabled={submitting || code.trim().length === 0}
          className="cursor-pointer rounded-full bg-[#d93900] px-5 py-2 text-white transition-colors hover:bg-[#c23300] disabled:cursor-not-allowed disabled:opacity-50 dark:bg-orange-600 dark:hover:bg-orange-700"
        >
          {submitting ? 'Checking…' : 'Verify'}
        </button>

        {resultTone && (
          <div
            role="status"
            className={`w-full rounded-lg border-2 p-3 text-center text-sm text-gray-800 dark:text-gray-100 ${toneStyles[resultTone]}`}
          >
            <p className="font-medium">{error ?? result?.message}</p>
            {requirements && (
              <ul className="mt-2 flex flex-col gap-1 text-left text-xs">
                {requirements.map((r) => (
                  <li key={r.label}>
                    {r.met ? '✅' : '❌'} <strong>{r.label}</strong> — {r.value}
                    {r.unit} <em>(need {r.threshold}{r.unit}+)</em>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </form>
    </div>
  );
};

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Splash />
  </StrictMode>
);
