# Plan: Focus-Extension-Ausfall → Copy-only-Falle (focus fix)

Stand: HEAD a926f1b (läuft: ~0177ab3). Read-only Analyse 2026-11-04. Kein Code geändert.

## 1. Root-Cause (verifiziert)

**Trigger:** kein Extensions-App-Toggle, sondern **Bildschirmsperre/Entsperren**. Stack im Journal 09:42:06:
`enable@extension.js:74 ← _callExtensionEnable ← _enableAllExtensions ← _sessionUpdated ← unlockDialog.js:937 finish`.
GNOME 46 deaktiviert beim Wechsel in den Session-Mode `unlock-dialog` alle Extensions ohne `"session-modes": [..., "unlock-dialog"]`
(metadata.json hat keine) und ruft beim Entsperren `enable()` erneut auf — also ein normaler disable→enable-Zyklus, der jeden Lock passiert.

**Bug:** `disable()` (extension.js:88) ruft `this._bus.unregister_object(OBJECT_PATH)` mit dem **Pfad-String**.
`Gio.DBusConnection.unregister_object()` erwartet die **Registration-ID (guint)**, die `register_object()` zurückgibt
(die wird in :74 verworfen). Der Aufruf wirft (oder deregistriert ID 0 = nichts) → vom leeren `catch` verschluckt
(„session ending anyway"). Folge:
1. `bus_unown_name` (:84) lief bereits → **Name `com.salmon.sttbutton` weg**.
2. Objekt bleibt auf der **geteilten** Session-Connection exportiert (`bus_get_sync` liefert das Singleton der Shell,
   überlebt jede Extension-Instanz).
3. Entsperren → `enable()` → `register_object` am selben Pfad/Interface → `G_IO_ERROR_EXISTS`
   „An object is already exported…" → Exception vor `bus_own_name` → GNOME setzt State **ERROR**
   und ruft nie wieder enable (nur Toggle/Re-Login). Die Zombie-Registration liegt bis Shell-Ende auf der Connection.

Warum heute erst: Live-Test 08:03 war vor dem ersten Lock; der erste Lock-Zyklus nach dem ESM-Rewrite (04.11.) war ~09:41.

**Symptom-Kette:** Diktat lief über den Lock/Entsperrzeitpunkt → Live-Deltas (wtype, fokusunabhängig) erschienen,
Finalisierung: `terminal_focused()`=None → `_final_mode()` (typing.py:647-665) → `copy` → Rest nur im Clipboard + Chime.
Danach jede Finalisierung `focus None -> copy`.

Ein Toggle in der Extensions-App behebt es **nicht** (Zombie-Registration bleibt → gleicher Fehler). Nur Logout/Login.

## 2. Micro-Blöcke

### Block F1 — extension.js idempotent + nie werfend (`tools/stt-button-focus@salmon.io/extension.js:51-92`)

```js
function _handle_method_call(connection, sender, object_path, interface_name,
                             method_name, parameters, invocation) {
    try {
        if (method_name === 'GetFocus') {
            const [app_id, title] = _focus_info();
            invocation.return_value(new GLib.Variant('(ss)', [app_id, title]));
        } else {
            invocation.return_error_literal(Gio.DBusError,
                Gio.DBusError.UNKNOWN_METHOD, `unknown method ${method_name}`);
        }
    } catch (e) {
        logError(e, '[stt-button-focus] GetFocus');
        try {
            invocation.return_error_literal(Gio.DBusError,
                Gio.DBusError.FAILED, String(e));
        } catch (_) { /* invocation already consumed */ }
    }
}
```
`_focus_info()`: zusätzlich `global.display?.get_focus_window?.()` in try → `['none','']`.

```js
export default class SttButtonFocusExtension {
    constructor(meta) { this.meta = meta; this._bus = null;
                        this._registration_id = 0; this._owned_name = 0; }

    enable() {
        this._teardown();                       // idempotent: second enable is clean
        try {
            this._bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);
            const node_info = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML);
            this._registration_id = this._bus.register_object(
                OBJECT_PATH, node_info.interfaces[0], _handle_method_call, null, null);
            this._owned_name = Gio.bus_own_name_on_connection(
                this._bus, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null,
                () => log('[stt-button-focus] name lost'));
            log('[stt-button-focus] ready (DBus com.salmon.sttbutton)');
        } catch (e) {
            logError(e, '[stt-button-focus] enable failed');
            this._teardown();                   // never leave a half registration
            throw e;                            // GNOME shows ERROR, but state is clean -> toggle recovers
        }
    }

    disable() { this._teardown(); }

    _teardown() {
        try { if (this._owned_name) Gio.bus_unown_name(this._owned_name); } catch (e) { logError(e); }
        this._owned_name = 0;
        try { if (this._bus && this._registration_id) this._bus.unregister_object(this._registration_id); }
        catch (e) { logError(e); }
        this._registration_id = 0;
        this._bus = null;
    }
}
```
Alle Felder pro Instanz, kein Modul-State. Optional (empfohlen, nicht nötig): `"session-modes": ["user","unlock-dialog"]`
in metadata.json → kein Zyklus bei Lock. Bewusst **nicht** als Fix (Review-Regel extensions.gnome.org verlangt Begründung;
der Fix muss den Zyklus ohnehin überleben). Nur als F1b falls F1-Live-Test wackelt.

**Offline-Test (gjs, kein Shell nötig)** `tools/test_focus_extension.js` (Skizze; `global` stubben):
```js
globalThis.global = { display: { get_focus_window: () => null } };
globalThis.log = print; globalThis.logError = (e, m) => print('ERR', m, e);
const { default: Ext } = await import('./stt-button-focus@salmon.io/extension.js');
const a = new Ext({});
a.enable(); a.disable(); a.enable();         // lock/unlock cycle: must not throw
a.enable();                                   // double enable: must not throw
const b = new Ext({}); a.disable(); b.enable(); b.disable(); b.disable(); // new instance, double disable
print('OK');
```
Lauf: `gjs -m tools/test_focus_extension.js` (RED auf altem Code: zweites enable wirft „already exported").
Plus `dbus-run-session -- gjs -m …` für isolierten Bus. Zusätzlich pytest-Wrapper `tests/test_focus_extension.py`
(`pytest.mark.skipif(shutil.which("gjs") is None)`, ruft obiges via subprocess, assert "OK" + rc 0) → P3 erfüllt.

### Block F2 — Fallback-Politik (`typing.py:647-665`, `config.py`, `app.py:102-118`)
Empfehlung **(a)+(c)**: Default bleibt `copy` (Terminal-Sicherheit: getippter Text + ggf. Enter-Risiko in Shell ist
schlimmer als ein Klick Ctrl+Shift+V), aber **nie still**:
- `config.py`: `focus_unknown_fallback: str = "copy"` (validate: in {"copy","type"}); config.example.toml + KONZEPT.md.
- `TerminalAwareTyper.__init__(..., focus_unknown_fallback="copy")`; in `_final_mode`: `state is None` und
  fallback=="type" → `"type"`.
- **WARN-Log** beim Übergang known→None (einmalig je Ausfallphase, nicht pro Diktat: Flag `_focus_unknown_warned`,
  reset bei nächstem bool) mit Hinweis `"focus extension unreachable – run tools/focus_check.py; final text -> clipboard"`.
- Sichtbares Signal: `notify-send -u critical "STT: Fokus unbekannt – Text nur in Zwischenablage"` beim ersten None
  (über bestehenden Notify-Pfad im Controller, falls vorhanden; sonst Callback `on_focus_unknown`). Kein neuer Ton.
(b) None→type allein abgelehnt: die Extension fällt genau dann aus, wenn man nichts merkt → Terminal bekommt getippten Text.

Tests (`tests/test_typing.py`-Muster, Fake-Paster mit `.copy`):
```python
def test_unknown_focus_default_copies():
    t = make_typer(focus_probe=lambda: None)          # existing helper/pattern
    assert t._final_mode() == "copy"

def test_unknown_focus_type_fallback():
    t = make_typer(focus_probe=lambda: None, focus_unknown_fallback="type")
    assert t._final_mode() == "type"

def test_unknown_focus_warns_once(caplog):
    t = make_typer(focus_probe=lambda: None)
    t._final_mode(); t._final_mode()
    assert sum("focus extension unreachable" in r.message for r in caplog.records) == 1

def test_warning_rearms_after_recovery(caplog): ...   # None, False, None -> 2 warnings
```
`tests/test_config.py`: invalid value → ValueError; default "copy".

### Block F3 — `tools/focus_check.py` (Operator/Monitoring)
Ein Aufruf, Exit-Code 0/1/2, Klartext:
1. `gnome-extensions info stt-button-focus@salmon.io` → State (ENABLED/ERROR/…).
2. `busctl --user status com.salmon.sttbutton` → Name-Owner vorhanden?
3. `stt_button.focus._call_focus()` → (app_id,title) oder None; `terminal_focused()`.
4. Bei ERROR: `gdbus call … org.gnome.Shell.Extensions.GetExtensionErrors` ausgeben.
5. Diagnose-Text: „ERROR + already exported → Zombie-Registration, nur Logout hilft" / „OK".
Test: `tests/test_focus_check.py` mit monkeypatched `subprocess.run` (3 Szenarien: ok, error, name missing).

### Block F4 (optional, diskutiert) — Watchdog
systemd-User-Timer, alle 5 min `focus_check.py --quiet`; bei Ausfall nur `notify-send` + Journal-WARN.
Automatische Reparatur unmöglich (Wayland: kein Shell-Reload; Toggle hilft beim alten Bug nicht, nach F1 schon:
`gnome-extensions disable/enable` wäre dann ein legitimer Auto-Heal). Empfehlung: **nicht bauen**; F2-WARN beim Diktat
deckt den relevanten Fall ab, F3 für manuelle Diagnose. Braucht ohnehin P5/P6-Freigabe (systemd).

## 3. Rollout

1. F1 (extension.js + gjs-Test) → Commit `focus: idempotent enable/disable, unregister by id`.
2. F2 → `focus: warn + configurable fallback on unknown focus`; F3 → `focus: add tools/focus_check.py`.
3. Volle Suite **erst nach Gate-Ende** (Benchmark läuft, nice 19); bis dahin nur `pytest tests/test_typing.py tests/test_config.py -q`
   wenn überhaupt — besser warten.
4. Extension installieren: `cp` nach `~/.local/share/gnome-shell/extensions/stt-button-focus@salmon.io/` (Shell lädt erst nach Login).
5. **Ein Logout/Login** (räumt Zombie-Registration ab, lädt F1) + Dienst-Restart (lädt Phase-2 + F2; P6: Operator-Freigabe).
6. Live-Verifikation: `focus_check.py` OK → Super+L, entsperren → `focus_check.py` erneut OK (das ist der eigentliche Regressionstest) →
   Diktat in Firefox (`focus False -> type`) und Terminal (`True -> copy`).

Sofort-Workaround bis dahin: Logout/Login (Toggle hilft nicht). Bis F1 aktiv ist: jeder Lock bricht es erneut.

**Konfliktcheck gegen a926f1b:** Phase-2 (0177ab3..a926f1b) berührt `config.py` (+31), `controller.py`, `app.py`(+7), nicht
`typing.py`, nicht `focus.py`, nicht `tools/stt-button-focus@salmon.io/`. F1/F3 konfliktfrei. F2 berührt config.py
(neues Feld am Ende der Dataclass, eigener validate-Zweig) und app.py:102-118 (Typer-Wiring) → auf a926f1b aufsetzen,
nach Gate-Push committen; kein Rebase-Risiko, da lineare Kette.

## 4. Nachtrag 09:55 — neue Operator-Evidenz („kurz ging es wieder, langes Diktat aus, Tab-Wechsel heilt")

### Prüfung der Hypothese „zweiter Laufzeitfehler (focus_window null)"
Journal-Fakten (`journalctl --user -b --since 09:30`):
- **0** Zeilen `focus True|False ->` seit 09:30; **jede** Finalisierung ab 09:43:59 ist `focus None -> copy` (15×, bis 09:47:34, auch nach Tab-Wechseln).
- Extension-State jetzt weiterhin **ERROR**, Name `com.salmon.sttbutton` nicht auf dem Bus → busctl rc≠0 → `_call_focus()`=None. Die Extension antwortet gar nicht, also kann sie auch kein `'none'` liefern.
- Ein Tab-Wechsel im selben Browser ändert in Mutter **nicht** das fokussierte MetaWindow (gleiches Toplevel) → könnte `get_focus_window()` ohnehin nicht beeinflussen.
- `win.get_actor()` null ist in `_focus_info()` bereits abgefangen (`actor && …` + try, Zeile 39-42); eine Exception im Handler würde nur bei `get_focus_window()` selbst entstehen (nicht beobachtet: keine Handler-Stacks im Journal).

**Ergebnis: Hypothese widerlegt für heute.** Es gibt nur EINEN Fehler (enable()-Kollision 09:42:06). Die Beobachtungen erklären sich vollständig durch die Ausgabearchitektur:
Live-Deltas werden fokusunabhängig per wtype getippt (11× `payload='typedelay…'` 09:42-09:48), nur der **Final-Rest** geht über `_final_mode()` → copy.
- Kurzes Diktat: fast alles kommt als Live-Delta → „geht wieder". Langes Diktat: großer, erst bei Finalisierung bestätigter Rest → „verschwindet" ins Clipboard.
- „Tab-Wechsel heilt" = Korrelation, keine Ursache (danach weiterhin None im Log).

### Zeitachse / Zuordnung
| Zeit | Ereignis | Operator-Beobachtung |
|---|---|---|
| 08:03–09:41 | Extension OK, `False->type` / `True->copy` | normal |
| ~09:41 | Lock → disable(): unown OK, unregister(Pfad) schlägt still fehl | — |
| 09:42:06 | Unlock → enable() → „already exported" → ERROR, Name weg | — |
| 09:43:59 ff. | jede Final: None→copy; Live-Deltas weiter getippt | (1) langes Diktat verliert Rest; (2) „nur Zwischenablage"; (3) kurze Diktate wirken ok, „Tab-Wechsel heilt" = Zufall der Diktatlänge |

### Trotzdem aufnehmen (Härtung, präventiv) — Block F1 erweitert
(a) `_focus_info()` voll exceptionssicher + **Last-Known-Cache**:
```js
// instance fields: this._last = null; this._last_ts = 0;   (pass ext to handler via closure)
_focus_info() {
    let win = null;
    try { win = global.display?.get_focus_window?.() ?? null; } catch (e) { win = null; }
    if (!win) {
        const age = GLib.get_monotonic_time() - this._last_ts;   // µs
        if (this._last && age < 5_000_000) return [...this._last, ];  // grace 5 s (overview, portal, popup)
        return ['none', ''];
    }
    ... existing app_id/title extraction ...
    this._last = [app_id || 'none', title]; this._last_ts = GLib.get_monotonic_time();
    return this._last;
}
```
Handler als Closure `(...a) => this._handle_method_call(...a)` (Instanz-State statt Modul-State). Grace 5 s: Overview/Portal-Dialog
ist kurz; länger als 5 s ohne Fenster → ehrlich 'none'. Risiko: Fokus wechselt in ein Terminal ohne MetaWindow — nicht möglich, also unkritisch.
(b) Handler global try/catch — bereits in F1.
(c) gjs-Mock-Tests (`tools/test_focus_extension.js`), zusätzlich:
```js
let fw = { get_actor: () => null, get_wm_class: () => 'firefox', get_title: () => 'T' };
global.display.get_focus_window = () => fw;            assert(ext._focus_info()[0] === 'firefox');   // null actor
global.display.get_focus_window = () => null;          assert(ext._focus_info()[0] === 'firefox');   // grace cache
ext._last_ts -= 6_000_000;                             assert(ext._focus_info()[0] === 'none');      // timeout
global.display.get_focus_window = () => { throw new Error('x'); }; assert(ext._focus_info()[0] === 'none' || cached);
fw.get_title = () => { throw new Error('gone'); };     // destroyed window: no throw, title ''
```

### Block F2b — focus.py: „Extension tot" vs. „kein Fenster" trennen (empfohlen, klein)
Neue Funktion `focus_state() -> Literal["terminal","other","no_window","unreachable"]`; `terminal_focused()` bleibt als Wrapper (API stabil, Tests grün).
`_final_mode()` trace: `latency[focus unreachable -> copy]` bzw. `no_window`. Nur `unreachable` triggert die F2-WARN/notify
(„Fokus-Extension tot – tools/focus_check.py"); `no_window` nur DEBUG. Damit hätte heute eine einzige WARN-Zeile um 09:43:59 die Ursache benannt.
Tests (`tests/test_focus.py`-Muster, monkeypatch `subprocess.run`): rc≠0 → unreachable; Timeout → unreachable; data `['none','']` → no_window;
`['org.gnome.Ptyxis','']` → terminal; `['firefox','x']` → other; `terminal_focused()` Mapping unverändert.
`focus_unknown_fallback` gilt für beide None-Arten (Default copy). Konflikt: focus.py/typing.py von Phase-2 unberührt.
