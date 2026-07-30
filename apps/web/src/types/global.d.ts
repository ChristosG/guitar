/**
 * Ambient declarations for globals this app does not create.
 *
 * `__GT_API_BASE__` is injected by the TAURI DESKTOP SHELL, not by any code in
 * this repo's `src/`: the shell picks two free ports at launch and hands the
 * page its API address through Tauri v2's `WebviewWindowBuilder::
 * initialization_script`, which runs BEFORE any page script. So from the web
 * bundle's point of view it is simply there — or, in the browser build, simply
 * absent. Optional (`?:`) is therefore the honest type: the webapp must keep
 * compiling, and behaving, with nothing injected at all.
 *
 * Declared here rather than cast at the single read site (`lib/api.ts`'s
 * `resolveApiBase`), because a lone `(window as any)` is exactly the kind of
 * cast that lets a typo (`__GT_API_URL__`) type-check and then silently fall
 * through to the wrong port at runtime.
 *
 * This file is a MODULE (`export {}`), which is what makes `declare global`
 * legal; `tsconfig.json`'s `include` globs every `.ts` under `apps/web`, so the
 * augmentation reaches the whole program without anyone importing it.
 */

export {};

declare global {
  interface Window {
    /** `"http://localhost:<apiport>"` — no trailing slash. Desktop only. */
    __GT_API_BASE__?: string;
  }
}
