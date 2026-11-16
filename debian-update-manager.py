#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Actualizador de Debian.

Aplicación GTK3 para revisar las actualizaciones disponibles de Debian,
elegir cuáles instalar y ejecutar la instalación con elevación vía pkexec.

Reglas de seguridad:
  * Solo consulta (lectura) hasta que el usuario confirme la instalación.
  * Nunca usa dist-upgrade/full-upgrade ni autoremove.
  * Usa ``apt-get install --no-remove`` para no desinstalar paquetes.
  * El modo simulación (``apt-get -s``) no toca el sistema.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

APP_ID = "DarioDrive.Software"
APP_NAME = "Actualizador de Debian"

PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?$")
APT_LIST_RE = re.compile(
    r"^(?P<name>\S+)/(?P<suite>\S+)\s+(?P<candidate>\S+)\s+(?P<arch>\S+)"
    r"(?:\s+\[upgradable from:\s*(?P<current>[^\]]+)\])?\s*$"
)

# Opciones de dpkg que evitan preguntas interactivas dentro de la ventana:
# se conserva la configuración local existente.
DPKG_SAFE_OPTS = [
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
]


@dataclass
class UpdatePackage:
    name: str
    current: str
    candidate: str
    suite: str = ""
    security: bool = False
    arch: str = ""

    @property
    def origin_label(self) -> str:
        if self.security:
            return "Seguridad"
        low = self.suite.lower()
        if "backports" in low:
            return "Backports"
        if "updates" in low or "proposed" in low:
            return "Actualizaciones"
        return self.suite or "Origen desconocido"


def is_security_suite(suite: str) -> bool:
    return "security" in suite.lower()


def normalize_suite(archives: Iterable[str]) -> str:
    """Une los archivos de origen de un paquete (p. ej. jammy-security)."""
    seen = []
    for archive in archives:
        if archive and archive not in seen:
            seen.append(archive)
    return ",".join(sorted(seen))


def parse_apt_list_upgradable(output: str) -> List[UpdatePackage]:
    """Interpreta la salida de ``LANG=C apt list --upgradable`` (modo respaldo)."""
    packages: List[UpdatePackage] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("Listing") or line.startswith("Listando"):
            continue
        match = APT_LIST_RE.match(line)
        if not match:
            continue
        suite = match.group("suite")
        packages.append(
            UpdatePackage(
                name=match.group("name"),
                current=(match.group("current") or "").strip(),
                candidate=match.group("candidate"),
                suite=suite,
                security=is_security_suite(suite),
                arch=match.group("arch"),
            )
        )
    packages.sort(key=lambda p: (not p.security, p.name.lower()))
    return packages


def list_updates_cli() -> List[UpdatePackage]:
    """Respaldo sin python3-apt: usa el listado de solo lectura de apt."""
    env = dict(os.environ, LANG="C", LC_ALL="C")
    try:
        proc = subprocess.run(
            ["apt", "list", "--upgradable"],
            capture_output=True, text=True, env=env, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"No se pudo consultar apt: {exc}") from exc
    return parse_apt_list_upgradable(proc.stdout)


def list_updates() -> List[UpdatePackage]:
    """Lista paquetes actualizables. Solo lectura, no modifica el sistema."""
    try:
        import apt  # type: ignore
    except ImportError:
        return list_updates_cli()

    cache = apt.Cache()
    packages: List[UpdatePackage] = []
    for pkg in cache:
        try:
            if not pkg.is_upgradable:
                continue
            candidate = pkg.candidate
            installed = pkg.installed
            if candidate is None or installed is None:
                continue
            archives = []
            for origin in candidate.origins:
                archive = getattr(origin, "archive", "") or ""
                if archive:
                    archives.append(archive)
            suite = normalize_suite(archives)
            packages.append(
                UpdatePackage(
                    name=pkg.name,
                    current=installed.version,
                    candidate=candidate.version,
                    suite=suite,
                    security=is_security_suite(suite),
                    arch=getattr(candidate, "architecture", "") or "",
                )
            )
        except Exception:  # paquetes rotos: se omiten, no rompen el listado
            continue
    packages.sort(key=lambda p: (not p.security, p.name.lower()))
    return packages


def demo_packages() -> List[UpdatePackage]:
    """Datos de ejemplo para probar la interfaz (no son reales)."""
    return [
        UpdatePackage("libssl3", "3.0.2-0ubuntu1.15", "3.0.2-0ubuntu1.18",
                      "jammy-security", True, "amd64"),
        UpdatePackage("linux-image-generic", "5.15.0.113.123", "5.15.0.119.129",
                      "jammy-security", True, "amd64"),
        UpdatePackage("firefox", "126.0+build1-0ubuntu0.22.04.1",
                      "128.0+build1-0ubuntu0.22.04.1", "jammy-updates", False, "amd64"),
        UpdatePackage("vim", "2:8.2.3995-1ubuntu2.16", "2:8.2.3995-1ubuntu2.17",
                      "jammy-updates", False, "amd64"),
        UpdatePackage("git", "1:2.34.1-1ubuntu1.10", "1:2.34.1-1ubuntu1.11",
                      "jammy-updates", False, "amd64"),
        UpdatePackage("libpython3.10", "3.10.12-1~22.04.3", "3.10.12-1~22.04.4",
                      "jammy-updates", False, "amd64"),
    ]


def validate_package_names(packages: Sequence[str]) -> List[str]:
    clean = [p.strip() for p in packages if p and p.strip()]
    if not clean:
        raise ValueError("No hay paquetes seleccionados.")
    invalid = [p for p in clean if not PKG_RE.match(p)]
    if invalid:
        raise ValueError("Nombre de paquete no válido: " + ", ".join(invalid))
    return clean


def _stdbuf_prefix() -> List[str]:
    if os.path.exists("/usr/bin/stdbuf"):
        return ["/usr/bin/stdbuf", "-oL", "-eL"]
    return []


def build_install_command(packages: Sequence[str], simulate: bool = False) -> List[str]:
    """Construye el comando de instalación.

    Simulación: apt-get -s como usuario, sin privilegios.
    Real: pkexec -> env -> apt-get install -y --no-remove.
    """
    names = validate_package_names(packages)
    if simulate:
        return ["apt-get", "-s", "install", "--no-remove", *DPKG_SAFE_OPTS, *names]
    has_pkexec = shutil_which("pkexec")
    if not has_pkexec:
        raise RuntimeError("pkexec no está instalado; no se puede elevar privilegios.")
    return [
        has_pkexec, "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive",
        *_stdbuf_prefix(),
        "apt-get", "install", "-y", "--no-remove",
        "-o", "APT::Color=0", "-o", "Dpkg::Progress-Fancy=0",
        *DPKG_SAFE_OPTS, *names,
    ]


def build_update_command() -> List[str]:
    """Construye ``apt-get update`` con elevación (solo catálogo, no instala)."""
    has_pkexec = shutil_which("pkexec")
    if not has_pkexec:
        raise RuntimeError("pkexec no está instalado; no se puede elevar privilegios.")
    return [
        has_pkexec, "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive",
        *_stdbuf_prefix(),
        "apt-get", "update", "-o", "APT::Color=0",
    ]


def shutil_which(name: str) -> Optional[str]:
    for directory in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None



POLKIT_AGENT_CANDIDATES = (
    "/usr/lib/policykit-1-gnome/polkit-gnome-authentication-agent-1",
    "/usr/libexec/polkit-gnome-authentication-agent-1",
    "/usr/lib/x86_64-linux-gnu/polkit-gnome-authentication-agent-1",
    "/usr/bin/xfce-polkit",
)


def ensure_polkit_agent() -> bool:
    """Intenta asegurar un agente de autenticación polkit en la sesión.

    pkexec lo necesita para mostrar el diálogo de contraseña. Devuelve True si
    ya había uno o si se pudo iniciar; no falla si no encuentra ninguno.
    """
    try:
        probe = subprocess.run(
            ["pgrep", "-f", "polkit-gnome-authentication-agent-1|xfce-polkit"],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    for path in POLKIT_AGENT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            subprocess.Popen([path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            continue
    return False


def summarize(packages: Sequence[UpdatePackage]) -> str:
    total = len(packages)
    security = sum(1 for p in packages if p.security)
    txt = "1 actualización" if total == 1 else f"{total} actualizaciones"
    if security:
        txt += f" · {security} de seguridad"
    return txt


# ---------------------------------------------------------------------------
# Interfaz gráfica
# ---------------------------------------------------------------------------

CSS = """
.pkg-name { font-weight: bold; }
.badge {
    border-radius: 6px;
    padding: 1px 8px;
    font-size: 90%;
    font-weight: bold;
}
.badge-security { background-color: #c01c28; color: #ffffff; }
.badge-updates { background-color: #1c71d8; color: #ffffff; }
.badge-other { background-color: #77767b; color: #ffffff; }
.dim { color: #5e5c64; }
.log-view { font-family: monospace; font-size: 90%; }
"""


def run_gui(demo: bool = False) -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gio, GLib, Gtk

    class AppWindow(Gtk.ApplicationWindow):
        def __init__(self, application: Gtk.Application) -> None:
            super().__init__(application=application, title=APP_NAME)
            self.set_default_size(900, 640)
            self.demo = demo
            self.packages: List[UpdatePackage] = []
            self.rows: List[Gtk.ListBoxRow] = []
            self.busy = False
            self.proc: Optional[subprocess.Popen] = None
            self.pulse_id: Optional[int] = None

            self._build_ui()
            self.refresh()

        # -- construcción de la interfaz -----------------------------------
        def _build_ui(self) -> None:
            header = Gtk.HeaderBar(show_close_button=True)
            header.set_title(APP_NAME)
            header.set_subtitle("Revisa y elige qué actualizar")

            self.reload_btn = Gtk.Button.new_from_icon_name(
                "view-refresh-symbolic", Gtk.IconSize.BUTTON)
            self.reload_btn.set_tooltip_text("Volver a consultar actualizaciones (solo lectura)")
            self.reload_btn.connect("clicked", lambda *_: self.refresh())
            header.pack_start(self.reload_btn)

            self.sim_switch = Gtk.Switch()
            self.sim_switch.set_active(self.demo)
            self.sim_switch.set_tooltip_text(
                "Modo simulación: muestra qué haría apt-get sin instalar nada")
            sim_box = Gtk.Box(spacing=6)
            sim_box.pack_start(Gtk.Label(label="Simular"), False, False, 0)
            sim_box.pack_start(self.sim_switch, False, False, 0)
            header.pack_end(sim_box)

            self.install_btn = Gtk.Button(label="Instalar seleccionadas")
            self.install_btn.get_style_context().add_class("suggested-action")
            self.install_btn.connect("clicked", self.on_install_clicked)
            header.pack_end(self.install_btn)

            self.set_titlebar(header)
            header.show_all()

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            self.add(outer)

            if self.demo:
                infobar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING,
                                      show_close_button=True)
                infobar.get_content_area().add(
                    Gtk.Label(label="Modo demostración: datos de ejemplo. No se instalará nada real."))
                outer.pack_start(infobar, False, False, 0)

            self.search_entry = Gtk.SearchEntry()
            self.search_entry.set_placeholder_text("Buscar paquete…")
            self.search_entry.connect("search-changed", lambda *_: self._apply_filter())

            topbar = Gtk.Box(spacing=12)
            topbar.set_margin_top(8)
            topbar.set_margin_bottom(8)
            topbar.set_margin_start(12)
            topbar.set_margin_end(12)
            self.select_all = Gtk.CheckButton(label="Seleccionar todo")
            self.select_all.connect("toggled", self.on_select_all)
            topbar.pack_start(self.select_all, False, False, 0)
            topbar.pack_start(self.search_entry, True, True, 0)
            self.count_label = Gtk.Label(label="")
            self.count_label.get_style_context().add_class("dim")
            topbar.pack_end(self.count_label, False, False, 0)
            outer.pack_start(topbar, False, False, 0)

            self.stack = Gtk.Stack()
            self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
            outer.pack_start(self.stack, True, True, 0)

            self.listbox = Gtk.ListBox()
            self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                                  valign=Gtk.Align.CENTER)
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.DIALOG)
            placeholder.pack_start(icon, False, False, 0)
            self.placeholder_label = Gtk.Label(label="Comprobando actualizaciones…")
            self.placeholder_label.get_style_context().add_class("dim")
            placeholder.pack_start(self.placeholder_label, False, False, 0)
            placeholder.show_all()
            self.listbox.set_placeholder(placeholder)

            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.add(self.listbox)
            self.stack.add_named(scroll, "list")

            empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                                valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
            empty_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.DIALOG)
            empty_icon.get_style_context().add_class("dim")
            empty_box.pack_start(empty_icon, False, False, 0)
            self.empty_title = Gtk.Label()
            self.empty_title.set_markup("<b>El sistema está al día</b>")
            empty_box.pack_start(self.empty_title, False, False, 0)
            self.empty_sub = Gtk.Label(
                label="No hay actualizaciones pendientes en el catálogo actual.")
            self.empty_sub.get_style_context().add_class("dim")
            empty_box.pack_start(self.empty_sub, False, False, 0)
            self.stack.add_named(empty_box, "empty")
            self.stack.set_visible_child_name("empty")

            # Zona de progreso y registro
            self.revealer = Gtk.Revealer()
            self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
            progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            progress_box.set_margin_start(12)
            progress_box.set_margin_end(12)
            progress_box.set_margin_bottom(6)
            self.progress = Gtk.ProgressBar()
            self.progress.set_show_text(False)
            progress_box.pack_start(self.progress, False, False, 0)
            self.progress_label = Gtk.Label(label="", xalign=0)
            self.progress_label.get_style_context().add_class("dim")
            progress_box.pack_start(self.progress_label, False, False, 0)

            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            log_scroll.set_size_request(-1, 150)
            self.log_view = Gtk.TextView()
            self.log_view.set_editable(False)
            self.log_view.set_cursor_visible(False)
            self.log_view.set_monospace(True)
            self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self.log_view.get_style_context().add_class("log-view")
            self.log_buffer = self.log_view.get_buffer()
            log_scroll.add(self.log_view)
            progress_box.pack_start(log_scroll, True, True, 0)
            self.revealer.add(progress_box)
            outer.pack_start(self.revealer, False, False, 0)

            footer = Gtk.Box(spacing=12)
            footer.set_margin_top(6)
            footer.set_margin_bottom(8)
            footer.set_margin_start(12)
            footer.set_margin_end(12)
            self.status_label = Gtk.Label(label="Listo", xalign=0)
            self.status_label.get_style_context().add_class("dim")
            footer.pack_start(self.status_label, True, True, 0)
            self.update_btn = Gtk.Button(label="Actualizar catálogo")
            self.update_btn.set_tooltip_text(
                "Ejecuta apt-get update para descargar la lista de paquetes (no instala nada)")
            self.update_btn.connect("clicked", self.on_update_catalog)
            footer.pack_end(self.update_btn, False, False, 0)
            outer.pack_start(footer, False, False, 0)

            provider = Gtk.CssProvider()
            provider.load_from_data(CSS.encode("utf-8"))
            screen = Gdk.Screen.get_default()
            if screen is not None:
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            self.connect("key-press-event", self._on_key)
            outer.show_all()
            self.revealer.set_reveal_child(False)

        def _on_key(self, _widget, event) -> bool:
            from gi.repository import Gdk
            key = event.keyval
            if key == Gdk.KEY_F5:
                self.refresh()
                return True
            if key == Gdk.KEY_q and event.state & Gdk.ModifierType.CONTROL_MASK:
                self.close()
                return True
            return False

        # -- listado -------------------------------------------------------
        def refresh(self) -> None:
            if self.busy:
                return
            self._set_busy(True, "Consultando actualizaciones…")
            self.placeholder_label.set_text("Consultando actualizaciones…")
            self.stack.set_visible_child_name("list")
            threading.Thread(target=self._refresh_worker, daemon=True).start()

        def _refresh_worker(self) -> None:
            try:
                if self.demo:
                    packages = demo_packages()
                else:
                    packages = list_updates()
                error = None
            except Exception as exc:  # se muestra en la interfaz
                packages, error = [], str(exc)
            from gi.repository import GLib
            GLib.idle_add(self._refresh_done, packages, error)

        def _refresh_done(self, packages, error) -> bool:
            self._set_busy(False)
            if error:
                self.placeholder_label.set_text("No se pudo consultar el sistema")
                self.empty_title.set_markup("<b>Error al consultar actualizaciones</b>")
                self.empty_sub.set_text(error)
                self.stack.set_visible_child_name("empty")
                self.status_label.set_text("Error: " + error)
                return False
            self.packages = packages
            self._populate()
            self.search_entry.set_text("")
            if not packages:
                self.empty_title.set_markup("<b>El sistema está al día</b>")
                self.empty_sub.set_text(
                    "No hay actualizaciones pendientes en el catálogo actual.\n"
                    "Prueba «Actualizar catálogo» si hace tiempo que no se refresca.")
                self.stack.set_visible_child_name("empty")
            else:
                self.stack.set_visible_child_name("list")
            self._update_counts()
            self.status_label.set_text("Listo · " + summarize(packages))
            return False

        def _populate(self) -> None:
            for child in self.listbox.get_children():
                self.listbox.remove(child)
            self.rows = []
            for pkg in self.packages:
                row = Gtk.ListBoxRow()
                row.pkg = pkg  # type: ignore[attr-defined]
                box = Gtk.Box(spacing=12)
                box.set_margin_top(6)
                box.set_margin_bottom(6)
                box.set_margin_start(10)
                box.set_margin_end(10)
                check = Gtk.CheckButton()
                check.connect("toggled", self._on_row_toggled)
                box.pack_start(check, False, False, 0)

                text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                name = Gtk.Label(xalign=0)
                name.set_text(pkg.name)
                name.get_style_context().add_class("pkg-name")
                text_box.pack_start(name, False, False, 0)
                versions = Gtk.Label(xalign=0)
                versions.set_markup(
                    f'<span foreground="#5e5c64" size="small">'
                    f'{GLib.markup_escape_text(pkg.current)} → '
                    f'{GLib.markup_escape_text(pkg.candidate)}'
                    f'{" · " + GLib.markup_escape_text(pkg.arch) if pkg.arch else ""}'
                    f'</span>')
                text_box.pack_start(versions, False, False, 0)
                box.pack_start(text_box, True, True, 0)

                badge = Gtk.Label(label=pkg.origin_label)
                badge.get_style_context().add_class("badge")
                badge.get_style_context().add_class(
                    "badge-security" if pkg.security
                    else "badge-updates" if "updates" in pkg.suite.lower()
                    else "badge-other")
                box.pack_end(badge, False, False, 0)
                row.add(box)
                row.check = check  # type: ignore[attr-defined]
                self.listbox.add(row)
                self.rows.append(row)
            self.listbox.show_all()

        def _apply_filter(self) -> None:
            needle = self.search_entry.get_text().strip().lower()
            visible = 0
            for row in self.rows:
                show = not needle or needle in row.pkg.name.lower()  # type: ignore[attr-defined]
                row.set_visible(show)
                if show:
                    visible += 1
            if needle and visible == 0:
                self.placeholder_label.set_text("Ningún paquete coincide con la búsqueda")

        def _update_counts(self) -> None:
            selected = sum(1 for r in self.rows if r.check.get_active())  # type: ignore[attr-defined]
            self.count_label.set_text(
                f"{summarize(self.packages)} · {selected} seleccionada{'s' if selected != 1 else ''}")
            self.install_btn.set_sensitive(not self.busy and selected > 0)

        def _on_row_toggled(self, _check) -> None:
            self._update_counts()

        def on_select_all(self, button) -> None:
            active = button.get_active()
            for row in self.rows:
                row.check.set_active(active)  # type: ignore[attr-defined]
            self._update_counts()

        # -- instalación ---------------------------------------------------
        def selected_packages(self) -> List[UpdatePackage]:
            return [r.pkg for r in self.rows if r.check.get_active()]  # type: ignore[attr-defined]

        def on_install_clicked(self, _button) -> None:
            if self.busy:
                return
            selected = self.selected_packages()
            if not selected:
                self._error_dialog("No hay paquetes seleccionados",
                                   "Marca al menos una actualización para instalar.")
                return
            simulate = self.sim_switch.get_active() or self.demo
            names = [p.name for p in selected]
            security = sum(1 for p in selected if p.security)
            detail = "Se instalarán:\n" + "\n".join("• " + n for n in names[:12])
            if len(names) > 12:
                detail += f"\n… y {len(names) - 12} más"
            detail += ("\n\nNo se ejecutará dist-upgrade ni autoremove. "
                       "apt-get no desinstalará paquetes (--no-remove).")
            if simulate:
                detail += "\n\nMODO SIMULACIÓN: no se modificará el sistema."
            else:
                detail += "\n\nSe pedirá la contraseña de administrador (pkexec)."

            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE,
                text=("Simular instalación" if simulate else "Instalar actualizaciones"))
            dialog.format_secondary_text(
                f"{len(names)} paquete{'s' if len(names) != 1 else ''}"
                + (f" ({security} de seguridad)" if security else ""))
            dialog.get_message_area().add(Gtk.Label(label=detail, xalign=0,
                                                    wrap=True, max_width_chars=60))
            dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
            dialog.add_button("Simular" if simulate else "Instalar", Gtk.ResponseType.OK)
            dialog.show_all()
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
            try:
                if simulate:
                    cmd = build_install_command(names, simulate=True)
                else:
                    cmd = build_install_command(names, simulate=False)
            except (ValueError, RuntimeError) as exc:
                self._error_dialog("No se puede iniciar", str(exc))
                return
            self._run_command(cmd, simulate, names)

        def on_update_catalog(self, _button) -> None:
            if self.busy:
                return
            if self.demo:
                self._error_dialog("Modo demostración",
                                   "El catálogo no se actualiza en modo demostración.")
                return
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                text="¿Actualizar el catálogo de paquetes?")
            dialog.format_secondary_text(
                "Ejecuta apt-get update: descarga la lista de paquetes disponibles. "
                "No instala ni modifica paquetes. Requiere contraseña de administrador.")
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
            try:
                cmd = build_update_command()
            except RuntimeError as exc:
                self._error_dialog("No se puede iniciar", str(exc))
                return
            self._run_command(cmd, False, [], catalog=True)

        def _run_command(self, cmd: List[str], simulate: bool,
                         names: List[str], catalog: bool = False) -> None:
            if not simulate:
                ensure_polkit_agent()
            self.log_buffer.set_text("")
            self.revealer.set_reveal_child(True)
            self._set_busy(True, "Ejecutando: " + " ".join(cmd[:4]) + "…")
            self._append_log("$ " + " ".join(cmd) + "\n\n")
            self.progress.pulse()
            self.pulse_id = GLib.timeout_add(150, self._pulse)
            threading.Thread(target=self._command_worker,
                             args=(cmd, simulate, names, catalog),
                             daemon=True).start()

        def _pulse(self) -> bool:
            self.progress.pulse()
            return True

        def _command_worker(self, cmd, simulate, names, catalog) -> None:
            rc = -1
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    GLib.idle_add(self._append_log, line)
                rc = self.proc.wait()
            except FileNotFoundError as exc:
                GLib.idle_add(self._append_log, f"Error: {exc}\n")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._append_log, f"Error inesperado: {exc}\n")
            finally:
                self.proc = None
            GLib.idle_add(self._command_done, rc, simulate, names, catalog)

        def _command_done(self, rc, simulate, names, catalog) -> bool:
            if self.pulse_id:
                GLib.source_remove(self.pulse_id)
                self.pulse_id = None
            self.progress.set_fraction(0.0)
            self._set_busy(False)
            if rc == 0:
                if simulate:
                    self.progress_label.set_text("Simulación completada.")
                    self.status_label.set_text("Simulación completada (no se instaló nada)")
                elif catalog:
                    self.progress_label.set_text("Catálogo actualizado.")
                    self.status_label.set_text("Catálogo actualizado")
                    self.refresh()
                else:
                    self.progress_label.set_text("Actualizaciones instaladas.")
                    self.status_label.set_text("Actualizaciones instaladas correctamente")
                    self._info_dialog(
                        "Actualizaciones instaladas",
                        f"Se instalaron correctamente {len(names)} paquete(s).")
                    self.refresh()
            else:
                if rc in (126, 127) and not simulate:
                    msg = ("No se pudo autenticar con pkexec. Puede que hayas cancelado "
                           "el diálogo o que no haya un agente polkit / bus de sistema "
                           "activo en la sesión.")
                else:
                    msg = f"El comando terminó con código {rc}."
                self.progress_label.set_text(msg)
                self.status_label.set_text("Error al ejecutar la operación")
                if not simulate:
                    self._error_dialog(
                        "La operación no terminó bien",
                        msg + "\n\nRevisa el registro en la ventana para ver el detalle.")
            return False

        def _append_log(self, text: str) -> bool:
            end = self.log_buffer.get_end_iter()
            self.log_buffer.insert(end, text)
            mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
            self.log_view.scroll_mark_onscreen(mark)
            return False

        # -- utilidades ----------------------------------------------------
        def _set_busy(self, busy: bool, message: str = "") -> None:
            self.busy = busy
            self.reload_btn.set_sensitive(not busy)
            self.update_btn.set_sensitive(not busy)
            self.select_all.set_sensitive(not busy)
            self.sim_switch.set_sensitive(not busy)
            if message:
                self.status_label.set_text(message)
            self._update_counts()

        def _info_dialog(self, title: str, text: str) -> None:
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK, text=title)
            dialog.format_secondary_text(text)
            dialog.run()
            dialog.destroy()

        def _error_dialog(self, title: str, text: str) -> None:
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK, text=title)
            dialog.format_secondary_text(text)
            dialog.run()
            dialog.destroy()

    class App(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(application_id=APP_ID,
                             flags=Gio.ApplicationFlags.FLAGS_NONE)

        def do_activate(self) -> None:
            win = self.get_active_window()
            if win is None:
                win = AppWindow(self)
            win.present()

    return App().run([sys.argv[0]])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_list(packages: Sequence[UpdatePackage]) -> None:
    for pkg in packages:
        flag = "SEGURIDAD" if pkg.security else pkg.origin_label
        origin = f" [{pkg.suite}]" if pkg.suite else ""
        print(f"{pkg.name:<28} {pkg.current} -> {pkg.candidate}  {flag}{origin}")
    print(f"\n{summarize(packages)}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--list", action="store_true",
                        help="muestra las actualizaciones y termina (solo lectura)")
    parser.add_argument("--demo", action="store_true",
                        help="usa datos de ejemplo para probar la interfaz")
    parser.add_argument("--simulate-install", nargs="+", metavar="PAQUETE",
                        help="simula la instalación de paquetes (apt-get -s, sin cambios)")
    args = parser.parse_args(argv)

    if args.simulate_install:
        try:
            cmd = build_install_command(args.simulate_install, simulate=True)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print("$ " + " ".join(cmd))
        return subprocess.call(cmd)

    if args.list:
        packages = demo_packages() if args.demo else list_updates()
        print_list(packages)
        return 0

    try:
        import gi  # noqa: F401
    except ImportError:
        print("Falta PyGObject (python3-gi). Instala: sudo apt install python3-gi "
              "gir1.2-gtk-3.0", file=sys.stderr)
        return 2
    return run_gui(demo=args.demo)


if __name__ == "__main__":
    sys.exit(main())
