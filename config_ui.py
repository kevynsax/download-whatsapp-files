import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
MAIN_SCRIPT = PROJECT_ROOT / "main.py"

FIELDS = [
    {
        "name": "WA_CHAT_NAME",
        "label": "Chat Name / Phone",
        "default": "61 9904-5559",
        "kind": "text",
        "help": "Exact chat title or phone string shown in WhatsApp.",
    },
    {
        "name": "WA_USER_DATA_DIR",
        "label": "User Data Folder",
        "default": "./wa_user_data",
        "kind": "path",
        "help": "Chrome profile folder used to keep your WhatsApp login.",
    },
    {
        "name": "DOWNLOADS_DIR",
        "label": "Downloads Folder",
        "default": "./downloads",
        "kind": "path",
        "help": "Where downloaded files are stored.",
    },
    {
        "name": "MAX_DOWNLOADS_PER_EXECUTION",
        "label": "Max Downloads Per Run",
        "default": "100",
        "kind": "int",
        "help": "Safety limit for one execution.",
    },
    {
        "name": "CLICK_WAIT_MS",
        "label": "Click Wait (ms)",
        "default": "1000",
        "kind": "int",
        "help": "Delay between right-click and download actions.",
    },
]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):].strip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
            value = value.replace('\\"', '"').replace('\\\\', '\\')

        if key:
            values[key] = value

    return values


def quote_env_value(value: str) -> str:
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def write_env_file(path: Path, values: dict[str, str]):
    lines = [
        "# WhatsApp downloader configuration",
        "# You can edit this file manually or with config_ui.py.",
        "",
    ]

    for field in FIELDS:
        name = field["name"]
        lines.append(f"{name}={quote_env_value(values.get(name, field['default']))}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def normalize_path_for_display(selected_dir: str) -> str:
    selected = Path(selected_dir).resolve()
    try:
        relative = selected.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return str(selected)

    rel_text = relative.as_posix()
    if rel_text == ".":
        return "./"
    return f"./{rel_text}"


def get_default_values() -> dict[str, str]:
    values = {field["name"]: field["default"] for field in FIELDS}
    values.update(parse_env_file(ENV_FILE))

    # Backward compatibility with older key used in docs.
    if "MAX_DOWNLOADS_PER_EXECUTION" not in values and "MAX_DOWNLOADS_PER_RUN" in values:
        values["MAX_DOWNLOADS_PER_EXECUTION"] = values["MAX_DOWNLOADS_PER_RUN"]

    return values


class ConfigApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WhatsApp Downloader Config")
        self.geometry("900x370")
        self.minsize(900, 370)
        self.maxsize(900, 370)

        self.values = get_default_values()
        self.variables: dict[str, tk.StringVar] = {}
        self.status_var = tk.StringVar(value=f"Config file: {ENV_FILE}")

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self, padding=16)
        main.grid(row=0, column=0, sticky="nsew")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        title = ttk.Label(main, text="WhatsApp Downloader - Settings", font=("Segoe UI", 14, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        subtitle = ttk.Label(
            main,
            text="Save values to .env and launch the downloader without editing Python files.",
        )
        subtitle.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))

        for index, field in enumerate(FIELDS, start=2):
            name = field["name"]
            label = ttk.Label(main, text=field["label"])
            label.grid(row=index, column=0, sticky="w", padx=(0, 12), pady=6)

            var = tk.StringVar(value=self.values.get(name, field["default"]))
            self.variables[name] = var

            entry = ttk.Entry(main, textvariable=var, width=68)
            entry.grid(row=index, column=1, sticky="ew", pady=6)

            if field["kind"] == "path":
                browse_btn = ttk.Button(
                    main,
                    text="Browse",
                    command=lambda n=name: self._browse_directory(n),
                    width=10,
                )
                browse_btn.grid(row=index, column=2, sticky="e", padx=(8, 0), pady=6)
            else:
                hint = ttk.Label(main, text=field["help"], foreground="#555555")
                hint.grid(row=index, column=2, sticky="w", padx=(8, 0), pady=6)

        main.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(main)
        btn_row.grid(row=len(FIELDS) + 2, column=0, columnspan=3, sticky="ew", pady=(14, 8))

        save_btn = ttk.Button(btn_row, text="Save .env", command=self.save_only)
        save_btn.grid(row=0, column=0, padx=(0, 8))

        run_btn = ttk.Button(btn_row, text="Run Downloader", command=self.save_and_run)
        run_btn.grid(row=0, column=1, padx=(0, 8))

        open_btn = ttk.Button(btn_row, text="Open Project Folder", command=self.open_project_folder)
        open_btn.grid(row=0, column=2)

        status = ttk.Label(main, textvariable=self.status_var, foreground="#0f5132")
        status.grid(row=len(FIELDS) + 3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _browse_directory(self, field_name: str):
        current_value = self.variables[field_name].get().strip()
        initial_dir = PROJECT_ROOT

        if current_value:
            candidate = Path(current_value)
            if not candidate.is_absolute():
                candidate = (PROJECT_ROOT / candidate).resolve()
            if candidate.exists():
                initial_dir = candidate

        selected = filedialog.askdirectory(initialdir=str(initial_dir))
        if selected:
            self.variables[field_name].set(normalize_path_for_display(selected))

    def _collect_and_validate(self) -> dict[str, str] | None:
        values: dict[str, str] = {}

        for field in FIELDS:
            name = field["name"]
            raw_value = self.variables[name].get().strip()
            if raw_value == "":
                messagebox.showerror("Invalid value", f"{name} cannot be empty.")
                return None

            if field["kind"] == "int":
                try:
                    parsed = int(raw_value)
                except ValueError:
                    messagebox.showerror("Invalid value", f"{name} must be an integer.")
                    return None

                if name == "MAX_DOWNLOADS_PER_EXECUTION" and parsed < 1:
                    messagebox.showerror("Invalid value", "MAX_DOWNLOADS_PER_EXECUTION must be >= 1.")
                    return None
                if name == "CLICK_WAIT_MS" and parsed < 0:
                    messagebox.showerror("Invalid value", "CLICK_WAIT_MS must be >= 0.")
                    return None

                raw_value = str(parsed)

            values[name] = raw_value

        return values

    def save_only(self):
        values = self._collect_and_validate()
        if values is None:
            return

        write_env_file(ENV_FILE, values)
        self.status_var.set(f"Saved config to {ENV_FILE}")
        messagebox.showinfo("Saved", ".env was saved successfully.")

    def build_run_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            # If packed as an exe, allow launching sibling downloader exe when available.
            frozen_path = Path(sys.executable)
            sibling_downloader = frozen_path.with_name("whatsapp_downloader.exe")
            if sibling_downloader.exists():
                return [str(sibling_downloader)]

        return [sys.executable, str(MAIN_SCRIPT)]

    def save_and_run(self):
        values = self._collect_and_validate()
        if values is None:
            return

        if not MAIN_SCRIPT.exists() and not getattr(sys, "frozen", False):
            messagebox.showerror("Missing file", f"Could not find {MAIN_SCRIPT}.")
            return

        write_env_file(ENV_FILE, values)

        env = os.environ.copy()
        env.update(values)

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

        try:
            subprocess.Popen(
                self.build_run_command(),
                cwd=str(PROJECT_ROOT),
                env=env,
                creationflags=creationflags,
            )
        except Exception as exc:
            messagebox.showerror("Launch failed", str(exc))
            return

        self.status_var.set("Downloader launched in a new window.")

    def open_project_folder(self):
        try:
            if os.name == "nt":
                os.startfile(str(PROJECT_ROOT))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(PROJECT_ROOT)])
            else:
                subprocess.Popen(["xdg-open", str(PROJECT_ROOT)])
        except Exception as exc:
            messagebox.showerror("Could not open folder", str(exc))


if __name__ == "__main__":
    app = ConfigApp()
    app.mainloop()
