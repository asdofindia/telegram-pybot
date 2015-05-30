import plugintypes
import json
import subprocess
import uuid
import shutil
import re
import sys

from pathlib import Path

import os
from os import path

import pip

from urllib.parse import urlparse

from tempfile import TemporaryFile

from telegrambot import git, auth


CENTRAL_REPO_URL="https://github.com/asdofindia/telegram-pybot-plugin-repo"
CENTRAL_REPO_NAME="main"

PKG_BASE_DIR="pkgs"
PKG_REPO_DIR="pkgs/repos"
PKG_TRASH_DIR="pkgs/trash"
PKG_INSTALL_DIR="pkgs/installed"

class GitResponse:
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg

class PackageManagerPlugin(plugintypes.TelegramPlugin):
    """
    telegram-pybot's package manager
    """
    patterns = {
        "^!pkg? (search) (.*)$": "search",
        "^!pkg? (install) (.*)$": "install",
        "^!pkg? (update)$": "update",
        "^!pkg? upgrade$": "upgrade_all",
        "^!pkg? upgrade ([\w-]+)$": "upgrade_pkg",
        "^!pkg? (uninstall) (.*)$": "uninstall",
        "^!pkg? (list)$": "list_installed",
        "^!pkg? (list all)$": "list_all",
    }

    usage = [
        "!pkg search <query>: Search the repo for packages",
        "!pkg update: Update the package repo cache",
        "!pkg upgrade [pkg_name]: Update to latest version of all or specified pkg",
        "!pkg install <package name>: Install a package",
        "!pkg uninstall <package name>: Uninstall a package",
        "!pkg list: List installed packages"
        "!pkg list_all: List packages in the repo"
    ]

    def _installed_repos(self):
        if path.exists(PKG_REPO_DIR):
            return os.listdir(PKG_REPO_DIR)
        return []

    def _repo_path(self, repo_name):
        return path.join(PKG_REPO_DIR, repo_name)

    def _load_repo_object(self, repo_name):
        repo_file = Path(PKG_REPO_DIR) / repo_name / "repo.json"
        try:
            with repo_file.open('r') as f:
                return json.load(f)
        except:
            print(sys.exc_info()[0])
        return None

    def _reload_repos(self, msg=None):
        pkg_repo_dir = Path(PKG_REPO_DIR)
        if not pkg_repo_dir.exists():
            pkg_repo_dir.mkdir(parents=True)

        self.repos = {}
        for repo in pkg_repo_dir.iterdir():
            repo_json = self._load_repo_object(repo.name)
            if repo_json:
                self.repos[repo.name] = repo_json
            elif msg:
                self.respond_to_msg(msg, "Error reloading repo: {}".format(repo.name))


    def activate_plugin(self):
        self.repos = {}
        if not path.exists(PKG_BASE_DIR):
            os.makedirs(PKG_BASE_DIR)
        if PKG_INSTALL_DIR not in self.plugin_manager.getPluginLocator().plugins_places:
            self.plugin_manager.updatePluginPlaces([PKG_INSTALL_DIR])
            self.reload_plugins()

        self._reload_repos()

    def _get_repo(self, repo_name):
        return self.repos.get(repo_name, None)

    def _pkg_data_from_repo(self, pkg_name, repo_name):
        for pkg in self.repos.get(repo_name, {}).get("packages",[]):
            if pkg["pkg_name"] == pkg_name:
                return pkg
        return None

    def _pkg_repo_path(self, pkg_name):
        return path.join(PKG_INSTALL_DIR, pkg_name)

    def _pkg_requirements_path(self, pkg_name):
        return path.join(self._pkg_repo_path(pkg_name), "repository", "requirements.txt")

    def __get_repo_json_from_repo_path(self, repo_path):
        repo_json_path = repo_json_path = path.join(repo_path, "repository", "repo.json")
        try:
            with open(repo_json_path, 'r') as f:
                return json.loads(f.read())
        except:
            pass
        return None

    @auth.authorize(groups=["admins"])
    def install(self, msg, matches):
        if not self.repos:
            self.respond_to_msg(msg, "Cannot locate repo. Try running \"!pkg update\"")

        repo_name = CENTRAL_REPO_NAME

        if not path.exists(PKG_INSTALL_DIR):
            os.makedirs(PKG_INSTALL_DIR)

        for pkg_name in matches.group(2).split():
            url = None
            pkg_data = None

            if urlparse(matches.group(2)).scheme in ["http", "https"]:
                url = pkg_name
            else:
                pkg_data = self._pkg_data_from_repo(pkg_name, repo_name)
                if not pkg_data:
                    self.respond_to_msg(msg, "Package not found in repository: {}".format(pkg_name))
                    return
                url = pkg_data["repo"]

            if not url:
                self.respond_to_msg(msg, "Invalid package name or url: {}".format(pkg_name))

            gs = git.clone(url, pkg_data["pkg_name"], cwd=PKG_INSTALL_DIR)
            if gs.has_error():
                self.respond_to_msg(msg, "Error installing package \"{}\"\n{}{}".format(pkg_name, gs.stdout, gs.stderr))
                return

            pkg_req_path = self._pkg_requirements_path(pkg_name)
            if os.path.exists(pkg_req_path):
                pip.main(['install', '-r', pkg_req_path])

            self.reload_plugins()
            for plugin_name in pkg_data.get("default_enable", []):
                self.plugin_manager.activatePluginByName(plugin_name)

            self.plugin_manager.collectPlugins()
            self.respond_to_msg(msg, "{}{}\nSuccessfully installed package: {}".format(gs.stdout, gs.stderr, pkg_name))

    def _upgrade_pkg(self, msg, pkg_name):
        pkg_path = Path(PKG_INSTALL_DIR) / pkg_name
        if not pkg_path.exists():
            self.respond_to_msg(msg, "Cannot upgrade \"{}\". Package does not appear to be installed.".format(pkg_name))

        gs = git.pull(str(pkg_path))
        self.respond_to_msg(msg, "{} {}: {}{}".format(gs.exit_status, pkg_name, gs.stdout, gs.stderr))


    @auth.authorize(groups=["admins"])
    def upgrade_all(self, msg, matches):
        if not path.exists(PKG_INSTALL_DIR):
            self.respond_to_msg(msg, "Nothing to update. It appears that there are no packages installed.")
            return

        for pkg_name in os.listdir(PKG_INSTALL_DIR):
            self._upgrade_pkg(msg, pkg_name)

    @auth.authorize(groups=["admins"])
    def upgrade_pkg(self, msg, matches):
        pkg_name = matches.group(1)
        self._upgrade_pkg(msg, pkg_name)

    @auth.authorize(groups=["admins"])
    def uninstall(self, msg, matches):
        install_dir = Path(PKG_INSTALL_DIR)
        trash_dir = Path(PKG_TRASH_DIR)

        if not trash_dir.exists():
            trash_dir.mkdir(parents=True)

        for pkg_name in matches.group(2).split():
            pkg_path = install_dir / pkg_name

            if not pkg_path.exists():
                continue

            trash_path = trash_dir / "{}.{}".format(pkg_name, str(uuid.uuid4()))
            pkg_path.rename(trash_path)
            self.respond_to_msg(msg, "Uninstalled package: {}".format(pkg_name))
            return

        self.respond_to_msg(msg, "Unable to find package: {}".format(pkg_name))

    def search(self, msg, matches):
        repo_name = CENTRAL_REPO_NAME
        repo = self._get_repo(repo_name)
        if not repo:
            self.respond_to_msg(msg, "Cannot locate repo. Try running \"!pkg update\"")
            return

        query = matches.group(2)
        prog = re.compile(query, flags=re.IGNORECASE)
        results = ""
        for pkg in repo.get("packages", []):
            if prog.search(pkg["name"]) or prog.search(pkg["description"]):
                results += "{} | {} | {}\n".format(pkg["pkg_name"], pkg["version"], pkg["description"])
        return results

    @auth.authorize(groups=["admins"])
    def update(self, msg, matches):
        repo_name = CENTRAL_REPO_NAME
        url = CENTRAL_REPO_URL
        pkg_repo_dir = Path(PKG_REPO_DIR)

        if not pkg_repo_dir.exists():
            pkg_repo_dir.mkdir(parents=True)

        gs = None
        if repo_name not in self._installed_repos():
            gs = git.clone(url, directory=repo_name, cwd=PKG_REPO_DIR)
        else:
            repo_path = self._repo_path(repo_name)
            git.reset(cwd=repo_path, hard=True)
            gs = git.pull(cwd=repo_path)

        if not gs:
            self.respond_to_msg(msg, "Unkown error updating repo: {}".format(repo_name))
            return

        if not gs.has_error():
            self._reload_repos(msg)

        self.respond_to_msg(msg, "{}: {}{}".format(repo_name, gs.stdout, gs.stderr))

    def list_all(self, msg, matches):
        repo_name = CENTRAL_REPO_NAME

        if repo_name not in self.repos.keys():
            self.respond_to_msg(msg, "Cannot locate repo. Try running \"!pkg update\".")
            return

        results = ""
        for pkg in self.repos.get(repo_name, {}).get("packages", []):
            results += "{} | {} | {}\n".format(pkg["pkg_name"], pkg["version"], pkg["description"])
        return results

    def list_installed(self, msg, matches):
        pkg_install_dir = Path(PKG_INSTALL_DIR)
        if not pkg_install_dir.exists():
            return "There are no packages installed"

        pkgs = ""
        for f in os.listdir(PKG_INSTALL_DIR):
            repo_path = os.path.join(PKG_INSTALL_DIR, f)
            repo_json = self.__get_repo_json_from_repo_path(repo_path)
            if repo_json:
                pkgs += "{} | {} | {}\n".format(f, repo_json["version"], repo_json["description"])
        return pkgs

    def reload_plugins(self):
        self.plugin_manager.collectPlugins()
        return "Plugins reloaded"
