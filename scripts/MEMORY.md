- 2026-08-16 (#111): **Round 2 of adversarial review: the bare-quantile gate was itself a bypass.**
  Skipping non-bare quantiles (added to stop `1000 * histogram_quantile(...) > 5000` red-building a
  correct rule) also skipped a merely PARENTHESISED quantile and one with a trailing `or vector(0)` -
  both in the histogram's own units, both then free to carry an inert `> 99999` with no annotation.
  A check whose escape hatch is shape-based grows bypasses the moment the shape test is coarser than
  the semantics. Fix: normalise before testing - strip guards, strip fully-enclosing parens (only
  when the leading `(` matches the final `)`, so `(a)+(b)` is untouched), and model `or vector(N)`
  exactly by adding N to the reachable set. N widens the range downward and CANNOT lift the ceiling,
  so `or vector(0)` makes a below-floor `<` legal while leaving an above-ceiling `>` just as inert.
  Second fix in the same area: families/quantiles were read from the RAW expression while bareness
  was read from the stripped one, so a quantile living inside the idle guard got range-checked
  against the rule's threshold - `> 10` on a 25.6 ceiling failed against the guard's 2.56 ceiling.
  Everything now reads the normalised expression.
- 2026-08-16 (#111): **Two small ones with a shared shape: the remediation must actually work.** The
  unknown-family violation told the author to add `<family> <top finite bucket bound>` - a 2-field
  line, which the 3-field regex silently DROPS, so following the message verbatim re-emits the
  byte-identical message forever. A guard whose error message prescribes a no-op is worse than no
  message. Also: `/*...*/` block comments and backtick raw strings were not stripped before the
  `Buckets:` scan, so a commented-out ladder above the live one derived (1,4) instead of (0.005,10) -
  the same wrong-bound-not-absent class the previous round closed for `//` and `"..."`, just the two
  lexical forms that round missed. `load_histogram_bounds` now also rejects non-finite and inverted
  bounds instead of accepting `foo nan inf`.
