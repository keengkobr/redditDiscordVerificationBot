import { afterEach, describe, expect, it, vi } from 'vitest';

let submitMutateMock: ReturnType<typeof vi.fn>;

vi.mock('@devvit/web/client', () => {
  return {
    context: {
      username: 'test-user',
    },
  };
});

vi.mock('./trpc', () => {
  submitMutateMock = vi.fn().mockResolvedValue({ ok: true, passed: true, message: 'Submitted!' });

  return {
    trpc: {
      verify: {
        submit: {
          mutate: submitMutateMock,
        },
      },
    },
  };
});

afterEach(() => {
  submitMutateMock?.mockReset();
  // splash.tsx renders on import (createRoot(...).render(...)); without
  // resetting the module registry, only the first `await import('./splash')`
  // across these tests would actually execute.
  vi.resetModules();
});

async function fillAndSubmit(code: string) {
  const input = document.querySelector('input') as HTMLInputElement;
  const form = document.querySelector('form') as HTMLFormElement;

  // React tracks the input's value via a native setter to detect real
  // changes; setting `.value` directly doesn't trip its change detection,
  // so bypass through the prototype setter the way React itself does.
  const nativeValueSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    'value'
  )!.set!;
  nativeValueSetter.call(input, code);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 0));
}

describe('Splash', () => {
  it('renders the verify form with the resolved username', async () => {
    document.body.innerHTML = '<div id="root"></div>';

    await import('./splash');
    await new Promise((r) => setTimeout(r, 0));

    expect(document.body.textContent).toContain('Verify for Discord');
    expect(document.body.textContent).toContain('test-user');
    expect(document.querySelector('input')).toBeTruthy();
  });

  it('submits the entered code via trpc.verify.submit', async () => {
    document.body.innerHTML = '<div id="root"></div>';

    await import('./splash');
    await new Promise((r) => setTimeout(r, 0));

    await fillAndSubmit('X7K2Q9');

    expect(submitMutateMock).toHaveBeenCalledWith({ code: 'X7K2Q9' });
  });

  it('shows the requirements checklist when the result includes metrics', async () => {
    document.body.innerHTML = '<div id="root"></div>';
    await import('./splash');
    await new Promise((r) => setTimeout(r, 0));

    // Override this test's resolved value on the same mock the freshly
    // (re-)imported splash.tsx is already wired to, rather than re-mocking
    // the module -- simpler than vi.doMock here since the mock factory
    // already ran for this test's fresh module registry.
    submitMutateMock.mockResolvedValueOnce({
      ok: true,
      passed: false,
      message: "Submitted. If this doesn't look right, you'll get a DM with next steps.",
      metrics: {
        accountAgeDays: 45,
        totalKarma: 87,
        subredditActivityCount: 3,
        subredditKarma: 3,
      },
      thresholds: {
        minAccountAgeDays: 30,
        minTotalKarma: 50,
        minSubredditActivityCount: 1,
        minSubredditKarma: 50,
      },
    });

    await fillAndSubmit('X7K2Q9');

    const text = document.body.textContent ?? '';
    expect(text).toContain('Account age');
    expect(text).toContain('Subreddit karma');
    expect(text.match(/✅/g)?.length).toBe(3);
    expect(text.match(/❌/g)?.length).toBe(1);
  });
});
