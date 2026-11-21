# TODO

## Hands-on checks for Matthew (please nag until done)

Matthew asked to be hassled about these (21 Nov 2026). Tick them off here.
Run PySynthRack from `.venv314` first (`$env:PYTHONPATH = "src"; .venv314\Scripts\python -m pysynthrack`),
then from the Exposure folder:

- [ ] **Browser panel:** `.venv\Scripts\python -m exposure panel <PID> --policy examples\pysynthrack.toml --preset panel`
      Move the "Osc 1 amp" slider: the synth's own amp slider and the sound should follow.
- [ ] **Trace:** `.venv\Scripts\python -m exposure shell <PID> --entry App.run`, then `trace self._mirror_param`,
      and move a slider in the panel or the synth: each call should appear live.
- [ ] **Logpoint / break when:** in the shell, `break when self.backend._xruns > 0` (pauses the GUI loop if audio
      ever drops out), and `log ui/app.py:<line> <expr>` on a line you care about.
- [ ] **snap / diff:** `snap before self.patch`, change something in the synth by hand, `diff before`.
- [ ] **Hot reload:** edit a small function in PySynthRack (or a scratch module), `reload <module>`, check it took,
      then `reload --undo`.
- [ ] **Shell polish:** `?self._mirror_param`, `src self._mirror_param`, `ls self *widget*`, Tab completion.
