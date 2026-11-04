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

function _focus_info() {
    const win = global.display.get_focus_window();
    if (!win)
        return ['none', ''];
    let app_id = '';
    try {
        const actor = win.get_actor();
        app_id = (actor && actor.get_app_id()) ? actor.get_app_id() : '';
    } catch (e) { app_id = ''; }
    if (!app_id) {
        try { app_id = win.get_wm_class() || ''; } catch (e) { app_id = ''; }
    }
    let title = '';
    try { title = win.get_title() || ''; } catch (e) { title = ''; }
    return [app_id || 'none', title];
}

function _handle_method_call(connection, sender, object_path,
                             interface_name, method_name,
                             parameters, invocation) {
    if (method_name === 'GetFocus') {
        const [app_id, title] = _focus_info();
        invocation.return_value(new GLib.Variant('(ss)', [app_id, title]));
    } else {
        invocation.return_error_literal(
            Gio.DBusError, Gio.DBusError.UNKNOWN_METHOD,
            `unknown method ${method_name}`);
    }
}

export default class SttButtonFocusExtension {
    constructor(meta) {
        this.meta = meta;
        this._bus = null;
        this._owned_name = null;
    }

    enable() {
        this._bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);
        const node_info = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML);
        this._bus.register_object(OBJECT_PATH, node_info.interfaces[0],
            _handle_method_call, null, null);
        this._owned_name = Gio.bus_own_name(
            Gio.BusType.SESSION, BUS_NAME,
            Gio.BusNameOwnerFlags.NONE, null, null, null);
        log('[stt-button-focus] ready (DBus com.salmon.sttbutton)');
    }

    disable() {
        if (this._owned_name !== null) {
            Gio.bus_unown_name(this._owned_name);
            this._owned_name = null;
        }
        try {
            this._bus?.unregister_object(OBJECT_PATH);
        } catch (e) { /* session ending anyway */ }
        this._bus = null;
    }
}
