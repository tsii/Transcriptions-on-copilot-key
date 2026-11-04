// STT Button Focus: exports the currently FOCUSED window (app-id, title)
// as the DBus method GetFocus() on the session bus.
// Purpose: stt-button decides per dictation whether to paste via chord or
// to only fill the clipboard (terminals need ctrl+shift+v from the user).
// The Kimi report (31.10.) showed that without a shell extension there is
// no reliable focus API under GNOME 4x/Wayland; this is the recommended way.
//
// 04.11. rewrite: GNOME 45+ loads extensions as ESM modules with a default
// exported class; the legacy init()/start() format is dead. GJS >= 1.77
// rejects per-field DBusMethodInfo construction and the old JS-vtable
// register_object form; the interface is therefore parsed from
// introspection XML and registered with the closure-based API.
// Verified end-to-end with gjs 1.80 (bus_get_sync + register_object +
// bus_own_name + GetFocus round trip via busctl, the exact call focus.py
// makes).
//
// F1 (plan_focus_fix.md): disable() used to unregister by PATH (wrong
// type, silently failed) -> zombie export -> unlock enable() hit
// "already exported" -> ERROR. Now: idempotent enable, unregister by
// ID, error-isolated handler, 5 s last-known-focus cache.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const BUS_NAME = 'com.salmon.sttbutton';
const OBJECT_PATH = '/com/salmon/sttbutton';
const IFACE_NAME = 'com.salmon.sttbutton.Focus';

const _INTROSPECTION_XML = `
<node>
  <interface name="${IFACE_NAME}">
    <method name="GetFocus">
      <arg type="s" direction="out"/>
      <arg type="s" direction="out"/>
    </method>
  </interface>
</node>`;

//: Grace period (µs) during which the last known focus is reported while
//: no window is focused (overview, portal dialog, popup).
const _GRACE_US = 5_000_000;

export default class SttButtonFocusExtension {
    constructor(meta) {
        // All state per instance: no module state survives a disable().
        this.meta = meta;
        this._bus = null;
        this._registration_id = 0;
        this._owned_name = 0;
        this._last = null;
        this._last_ts = 0;
    }

    _focus_info() {
        let win = null;
        try {
            win = global.display?.get_focus_window?.() ?? null;
        } catch (e) { win = null; }
        if (!win) {
            const age = GLib.get_monotonic_time() - this._last_ts;
            if (this._last && age < _GRACE_US)
                return [...this._last];
            return ['none', ''];
        }
        let app_id = '';
        try {
            const actor = win.get_actor?.();
            app_id = (actor && actor.get_app_id) ? (actor.get_app_id() || '') : '';
        } catch (e) { app_id = ''; }
        if (!app_id) {
            try { app_id = win.get_wm_class() || ''; } catch (e) { app_id = ''; }
        }
        let title = '';
        try { title = win.get_title() || ''; } catch (e) { title = ''; }
        this._last = [app_id || 'none', title];
        this._last_ts = GLib.get_monotonic_time();
        return [...this._last];
    }

    _handle_method_call(connection, sender, object_path, interface_name,
                        method_name, parameters, invocation) {
        try {
            if (method_name === 'GetFocus') {
                const [app_id, title] = this._focus_info();
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

    enable() {
        // Idempotent: GNOME re-enables extensions on every screen unlock;
        // a second enable() must start from a clean state.
        this._teardown();
        try {
            this._bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);
            const node_info = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML);
            this._registration_id = this._bus.register_object(
                OBJECT_PATH, node_info.interfaces[0],
                (...a) => this._handle_method_call(...a), null, null);
            this._owned_name = Gio.bus_own_name_on_connection(
                this._bus, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null,
                () => log('[stt-button-focus] name lost'));
            log('[stt-button-focus] ready (DBus com.salmon.sttbutton)');
        } catch (e) {
            logError(e, '[stt-button-focus] enable failed');
            this._teardown();   // never leave a half registration behind
            throw e;            // GNOME shows ERROR, state is clean -> toggle recovers
        }
    }

    disable() {
        this._teardown();
    }

    _teardown() {
        try {
            if (this._owned_name)
                Gio.bus_unown_name(this._owned_name);
        } catch (e) { logError(e, '[stt-button-focus] unown'); }
        this._owned_name = 0;
        try {
            // unregister_object() takes the registration ID, NOT the path.
            if (this._bus && this._registration_id)
                this._bus.unregister_object(this._registration_id);
        } catch (e) { logError(e, '[stt-button-focus] unregister'); }
        this._registration_id = 0;
        this._bus = null;
    }
}
