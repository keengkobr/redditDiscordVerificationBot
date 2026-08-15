import './index.css';

import { navigateTo, context } from '@devvit/web/client';
import { StrictMode, useState } from 'react';
import type { FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { trpc } from './trpc';

export const Splash = () => {
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!code.trim()) return;
    setSubmitting(true);
    setMessage(null);
    try {
      const result = await trpc.verify.submit.mutate({ code: code.trim() });
      setMessage(result.message);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : 'Something went wrong -- try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center gap-4 bg-white p-4 dark:bg-gray-900">
      <img
        className="mx-auto w-1/2 max-w-[160px] object-contain"
        src="/snoo.png"
        alt="Snoo"
      />
      <div className="flex flex-col items-center gap-2 text-center">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Verify for Discord</h1>
        <p className="max-w-sm text-base text-gray-600 dark:text-gray-300">
          Hey {context.username ?? 'there'} — paste the code from your Discord DM below to link
          your accounts.
        </p>
      </div>
      <form onSubmit={submit} className="flex flex-col items-center gap-2">
        <input
          className="w-48 rounded border border-gray-300 px-3 py-2 text-center text-black"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="e.g. X7K2Q9"
          maxLength={12}
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
        {message && (
          <p className="max-w-sm text-center text-sm text-gray-700 dark:text-gray-300" role="status">
            {message}
          </p>
        )}
      </form>
      <footer className="absolute bottom-4 left-1/2 flex -translate-x-1/2 gap-3 text-[0.8em] text-gray-600 dark:text-gray-400">
        <button
          className="cursor-pointer hover:text-gray-900 dark:hover:text-white transition-colors"
          onClick={() => navigateTo('https://developers.reddit.com/docs')}
        >
          Docs
        </button>
      </footer>
    </div>
  );
};

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Splash />
  </StrictMode>
);
