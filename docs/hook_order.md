# Hook dependency order

## The bug this prevents

A hook dependency array is evaluated **during render**, not when the effect runs:

```jsx
useEffect(() => {
  fetchActionCatalogue()
}, [fetchActionCatalogue])            // <- read here, on the first render

const fetchActionCatalogue = useCallback(...)   // <- declared below
```

Reading a `const` before its declaration throws
`ReferenceError: Cannot access 'fetchActionCatalogue' before initialization`.
The bundler cannot see it, ESLint's React rules do not check it, and a
production build still succeeds — the component only explodes the moment a user
opens it. That is exactly how the world studio shipped broken: every other panel
worked, only the `actions` tab raised "服务出现异常".

## The guard

`frontend/scripts/check-hook-order.mjs` walks `frontend/src`, records the first
declaration line of every **component-scope** binding (two-space indent), and
fails when a hook dependency array names something declared further down the
same file.

It runs as the first step of `npm run build`:

```
"build": "node scripts/check-hook-order.mjs && vite build"
```

and can be run alone with `npm run check:hooks`.

Restricting the scan to two-space indentation is what keeps it honest: helpers
declared deeper inside a component or inside another component sit in a
different scope and are not reported. A violation looks like:

```
Hook dependency order problems (these throw "Cannot access before initialization"):
  src/components/WorldStudio.jsx:1156 depends on 'fetchActionCatalogue' declared later at line 1263
```

## Verified

- The real defect in `WorldStudio.jsx` was reported, then fixed by moving the
  `useCallback` above the effect.
- A synthetic fixture with the same shape was reported (`src/__hook_order_probe.jsx:7
  depends on 'load' declared later at line 9`) and then deleted.
- `npm run build` is green: the checker prints `hook dependency order: ok` before Vite builds.
