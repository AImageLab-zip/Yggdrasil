/**
 * An empty stand-in for `fs` and `path` inside emscripten codec glue.
 *
 * `@cornerstonejs/codec-charls` and its siblings ship one glue file for both Node and
 * the browser, and the Node half `require`s `fs` and `path` behind an
 * `ENVIRONMENT_IS_NODE` guard that is false in a browser. esbuild does not evaluate the
 * guard, so it tries to resolve the imports and fails the build outright.
 *
 * Resolving them to this module is safe *because* of that guard: the branch that would
 * touch these bindings never runs. It is aliased only for importers inside the DICOM
 * codec packages (see `nodeBuiltinShimPlugin`), so an accidental `import 'fs'` anywhere
 * in our own frontend still fails the build the way it should.
 */
export default {};
