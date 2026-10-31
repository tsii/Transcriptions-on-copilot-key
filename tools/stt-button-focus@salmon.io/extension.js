// STT Button Focus: exportiert das aktuell FOKUSSIERTE Fenster (app-id,
// Titel, PID) als DBus-Methode GetFocus() auf dem Session-Bus.
// Zweck: stt-button soll den Fertig-Ton nur erlaeuschen, wenn ein
// Terminal im Fokus ist (dort muessen manuell Strg+Umschalt+V).
// Kimi-Gutachten 31.10.: ohne Shell-Erweiterung ist unter GNOME 4x/Wayland
// keine zuverlaessige Fokus-API moeglich; dies ist der empfohlene Weg.

const { Gio, GLib } = imports.gi;

const OBJECT_PATH = '/com/salmon/sttbutton';
const IFACE_NAME = 'com.salmon.sttbutton.Focus';

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

function _on_method_invocation(connection, sender, object_path,
                                interface_name, method_name,
                                parameters, invocation) {
    if (method_name === 'GetFocus') {
        const [app_id, title] = _focus_info();
        invocation.return_value(new GLib.Variant('(ss)', [app_id, title]));
    } else {
        invocation.return_error_new(
            new GLib.Error(Gio.DBusError, Gio.DBusError.UNKNOWN_METHOD,
                           'unbekannte Methode %s', method_name));
    }
}

const _iface = new Gio.DBusInterfaceInfo({
    name: IFACE_NAME,
    methods: [
        new Gio.DBusMethodInfo({
            name: 'GetFocus',
            in_signature: '',
            out_signature: '(ss)',
            arg_names: [],
        }),
    ],
    signals: [],
    properties: [],
});

let _owned_name = null;

class SttButtonFocus {
    enable() {
        this._bus = Gio.DBusConnection.new_for_bus_sync(
            Gio.BusType.SESSION, null, null);
        this._bus.register_object(OBJECT_PATH, _iface, {
            handleMethodInvocation: _on_method_invocation,
        });
        this._bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'RequestName',
            new GLib.Variant('(su)', ['com.salmon.sttbutton', 0]),
            null, Gio.DBusCallFlags.NONE, -1, null);
        log(`[stt-button-focus] bereit (DBus com.salmon.sttbutton)`);
    }

    disable() {
        try {
            this._bus.unregister_object(OBJECT_PATH);
        } catch (e) { /* Session endet sowieso */ }
    }
}

function init() {}
function start() {
    return new SttButtonFocus();
}
function stop() {}
