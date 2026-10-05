# Bundled apps

Apps that ship inside the Asgard zip go here, one folder per app, for example `apps/freya/freya.pyw`.

To give a bundled app a tile, edit its entry in `asgard/apps.json`. Set `"status": "available"` and point `launch` at the app through the `{app}` placeholder:

```json
"launch": {"type": "python", "target": "{app}\\apps\\freya\\freya.pyw"}
```

Apps installed elsewhere, such as Odin, don't go here. Their tiles use `"status": "external"`, and each person picks the location the first time they click the tile.
