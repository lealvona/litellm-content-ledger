# Plugins

Plugins let you add an exporter, importer or notifier without forking this project. They are **off by
default**.

## Trust model

A plugin is ordinary Python code that runs with the permissions of the process that loads it. Nobody can
push a plugin onto your system. One only runs if you install it and name it in your own profile with
`[plugins] allow = true`. Treat a plugin exactly like any package you `pip install`.

What a plugin can and cannot reach is fixed by where it loads:

- **Never inside the LiteLLM proxy.** The capture callback does not read the profile and has no loader. A
  plugin can never see provider API keys, unredacted requests in flight, or the proxy's event loop.
- **Exporters** receive records after secret redaction.
- **Importers** produce records. Those records are validated, then redacted by the ship stage before storage.
- **Notifiers** receive health events: kind, severity, summary and numeric attributes. They never receive
  content.

Redaction removes known secret formats. It does not remove personal data, because the content is the
record. Only export to destinations you would trust with your prompts.

## Interfaces

```python
class Exporter:            # [[export]] type = "plugin"
    def export(self, records: Iterable[dict]) -> int: ...      # returns records written
    def close(self) -> None: ...                                # optional

class Importer:            # [[import]] type = "plugin"
    def records(self) -> Iterator[dict]: ...                   # schema v1 records

class Notifier:            # [[notify]] type = "plugin"
    def notify(self, event: dict) -> None: ...
    # event = {"kind": str, "severity": "info"|"warning"|"critical", "summary": str, "attrs": {str: number|str}}
```

The class is constructed with the entry's other keys as keyword arguments. A key ending in `_env` is read
from that environment variable first, and a missing variable is an error:

```toml
[plugins]
allow = true
paths = ["/etc/content-ledger/plugins"]   # optional extra import paths

[[notify]]
type = "plugin"
entry = "my_alerts:PagerNotifier"
routing_key_env = "PAGER_KEY"             # becomes PagerNotifier(routing_key=os.environ["PAGER_KEY"])
```

A plugin that raises is reported and skipped. It never stops the other exporters or notifiers, or the ship
itself.
