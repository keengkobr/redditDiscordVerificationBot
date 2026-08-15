import { afterEach, describe, expect, it, vi } from 'vitest';

let navigateToMock: ReturnType<typeof vi.fn>;
let submitMutateMock: ReturnType<typeof vi.fn>;

vi.mock('@devvit/web/client', () => {
  navigateToMock = vi.fn();

  return {
    navigateTo: navigateToMock,
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
  navigateToMock?.mockReset();
  submitMutateMock?.mockReset();
  // splash.tsx renders on import (createRoot(...).render(...)); without
  // resetting the module registry, only the first `await import('./splash')`
  // across these tests would actually execute.
  vi.resetModules();
});

describe('Splash', () => {
  it('renders the verify form with the resolved username', async () => {
    document.body.innerHTML = '<div id="root"></div>';

    await import('./splash');
    await new Promise((r) => setTimeout(r, 0));

    expect(document.body.textContent).toContain('Verify for Discord');
    expect(document.body.textContent).toContain('test-user');
    expect(document.querySelector('input')).toBeTruthy();
  });

  it('clicking the "Docs" footer button calls navigateTo(...)', async () => {
    document.body.innerHTML = '<div id="root"></div>';

    await import('./splash');
    await new Promise((r) => setTimeout(r, 0));

    const docsButton = Array.from(document.querySelectorAll('button')).find((b) =>
      /docs/i.test(b.textContent ?? '')
    );
    expect(docsButton).toBeTruthy();

    docsButton!.click();

    expect(navigateToMock).toHaveBeenCalledTimes(1);
    expect(navigateToMock).toHaveBeenCalledWith('https://developers.reddit.com/docs');
  });

  it('submits the entered code via trpc.verify.submit', async () => {
    document.body.innerHTML = '<div id="root"></div>';

    await import('./splash');
    await new Promise((r) => setTimeout(r, 0));

    const input = document.querySelector('input') as HTMLInputElement;
    const form = document.querySelector('form') as HTMLFormElement;

    // React tracks the input's value via a native setter to detect real
    // changes; setting `.value` directly doesn't trip its change detection,
    // so bypass through the prototype setter the way React itself does.
    const nativeValueSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      'value'
    )!.set!;
    nativeValueSetter.call(input, 'X7K2Q9');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

    await new Promise((r) => setTimeout(r, 0));

    expect(submitMutateMock).toHaveBeenCalledWith({ code: 'X7K2Q9' });
  });
});
