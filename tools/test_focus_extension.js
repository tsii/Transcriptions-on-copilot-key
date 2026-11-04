// Offline test for stt-button-focus@salmon.io/extension.js (no shell needed).
// Run isolated: dbus-run-session -- gjs -m tools/test_focus_extension.js
// Simulates GNOME's `global.display` and the lock/unlock enable cycle.

globalThis.global = { display: { get_focus_window: () => null } };
globalThis.logError = (e, m) => print('LOGERR', m ?? '', e);

const { default: Ext } = await import('./stt-button-focus@salmon.io/extension.js');

function assert(cond, msg) {
    if (!cond) { print('FAIL', msg); imports.system.exit(1); }
}

// Lifecycle: lock/unlock cycle, double enable, new instance, double disable.
const a = new Ext({});
a.enable(); a.disable(); a.enable();
a.enable();
const b = new Ext({});
a.disable(); b.enable(); b.disable(); b.disable();

// Focus info hardening.
const ext = new Ext({});
const fw = { get_actor: () => null, get_wm_class: () => 'firefox', get_title: () => 'T' };
global.display.get_focus_window = () => fw;
assert(ext._focus_info()[0] === 'firefox', 'null actor -> wm_class');
global.display.get_focus_window = () => null;
assert(ext._focus_info()[0] === 'firefox', 'grace cache within 5 s');
ext._last_ts -= 6_000_000;
assert(ext._focus_info()[0] === 'none', 'cache expires after 5 s');
global.display.get_focus_window = () => { throw new Error('x'); };
assert(ext._focus_info()[0] === 'none', 'throwing get_focus_window -> none');
global.display = null;
assert(ext._focus_info()[0] === 'none', 'null display -> none');
global.display = { get_focus_window: () => fw };
fw.get_title = () => { throw new Error('gone'); };
const r = ext._focus_info();
assert(r[0] === 'firefox' && r[1] === '', 'destroyed window: title empty');

// Handler never throws, even if invocation.return_value throws.
let errs = 0;
const inv = {
    return_value: () => { throw new Error('consumed'); },
    return_error_literal: () => { errs++; },
};
ext._handle_method_call(null, '', '', '', 'GetFocus', null, inv);
assert(errs === 1, 'handler maps exception to DBus error');
ext._handle_method_call(null, '', '', '', 'Nope', null, inv);
assert(errs === 2, 'unknown method -> error');

print('OK');
