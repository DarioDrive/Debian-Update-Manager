# Debian Update Manager

A GTK3 desktop application for Debian and Ubuntu that allows users to check, review, and selectively install package updates through a graphical interface.

## Features

- **Update Detection:** Check for available package updates using APT.
- **Individual Package Selection:** Choose which packages to update.
- **Security Update Highlighting:** Easily identify security-related updates.
- **Package Search:** Find specific packages in the update list.
- **Select All:** Select all available updates at once.
- **Simulation Mode:** Preview installation operations before making changes.
- **Safe Installation:** Uses `apt-get install --no-remove` to prevent package removals.
- **Explicit Confirmation:** Review and confirm operations before installation.
- **Polkit Authentication:** Request administrator privileges through the system authentication dialog.
- **Live Operation Log:** Monitor package management operations.
- **Progress Indicator:** Track ongoing operations.
- **Package Catalog Refresh:** Refresh the APT package catalog from the application.
- **Command-Line Interface:** List available updates and run simulation commands.
- **Demo Mode:** Explore the interface using sample package data.

## Safety Principles

Debian Update Manager is designed to provide greater control over package updates.

- Checking available updates does not modify installed packages.
- Installation requires explicit user confirmation.
- Package installation uses `--no-remove` to prevent automatic package removals.
- The application does not run `dist-upgrade` or `full-upgrade`.
- The application does not run `autoremove`.
- Simulation mode allows users to review proposed changes before installation.
- Existing configuration files are handled using conservative `dpkg` options.

## Tested Platforms

The application has been tested on:

- Debian 12
- Debian 13
- Ubuntu 22.04 (including minimal container environments)

## Requirements

- Debian or Ubuntu-based Linux distribution using APT.
- Python 3.
- GTK 3.
- PyGObject.
- python3-apt.
- APT package management tools.
- `pkexec` and a working Polkit authentication agent for graphical installation operations.

## Dependencies

On Debian or Ubuntu, install the required packages with:

```bash
sudo apt update
sudo apt install python3 python3-gi gir1.2-gtk-3.0 python3-apt policykit-1
```

A working Polkit authentication agent is required for graphical operations that need administrator privileges. Some minimal desktop or container environments may require additional configuration.

## Installation

Clone the repository:

```bash
git clone https://github.com/DarioDrive/Debian-Update-Manager.git
```

Enter the project directory:

```bash
cd Debian-Update-Manager
```

Run the application:

```bash
python3 debian-update-manager.py
```

## Usage

Launch the application and refresh the package catalog to check for available updates.

Select the packages you want to install, review the selection, and confirm the operation.

You can also use simulation mode to preview the proposed changes without installing packages.

## Command-Line Options

List available updates:

```bash
python3 debian-update-manager.py --list
```

Launch demo mode:

```bash
python3 debian-update-manager.py --demo
```

Simulate the installation of a package:

```bash
python3 debian-update-manager.py --simulate-install PACKAGE_NAME
```

Multiple packages can also be specified.

## License

This project is licensed under the GNU General Public License v2.0.

See the [LICENSE](LICENSE) file for details.

## Author

**DarioDrive**

GitHub: https://github.com/DarioDrive
