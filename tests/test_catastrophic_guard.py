"""Предохранитель YOLO ловит катастрофы, а не любой rm с абсолютным путём.

17.10: подстрока «rm -rf /» срабатывала на «rm -rf /tmp/sh_gate». Три
ложных стопа за час; два без ответа за 120 с — команды отклонены, а
владелец решил, что YOLO выключился.
"""

from __future__ import annotations

import pytest

from app.permissions import is_catastrophic_bash

DANGEROUS = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf ~/",
    "rm -rf $HOME",
    "rm -rf /etc",
    "rm -rf /var/",
    "sudo rm -rf / --no-preserve-root",
    "cd x && rm -rf /",
    "rm -fr /root",
    "rm -r -f /home",
    "rm -Rf /usr",
    "rm --recursive -f /",
    "shutdown -h now",
    "sudo reboot",
    "x; halt",
    "systemctl reboot",
    "echo $(poweroff)",
    "mkfs.ext4 /dev/sda1",
    "userdel bob",
    "chmod -R 000 /",
    "chmod 000 /",
    "dd if=/dev/zero of=/dev/sda",
    ":(){:|:&};:",
    "iptables -F",
    "ufw disable",
    "passwd root",
]

SAFE = [
    "rm -rf /tmp/sh_gate",  # тот самый ложный стоп 17.10
    "git checkout -- x && rm -rf /tmp/sh_gate && git worktree prune",
    "rm -rf /var/tmp/x",
    "rm -rf /opt/slot-hunter/.pytest_cache",
    "rm -rf ~/projects/x",
    "rm -rf ./build",
    "grep -n on_shutdown app/",
    "grep -rn 'shutdown' app/workers",
    "echo asphalt",
    "ls /home/user",
    "chmod 000 /tmp/x",
    "docker compose restart worker",
    "sed -n 1,5p reboot_notes.md",
]


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_catastrophes_are_still_stopped(cmd: str) -> None:
    assert is_catastrophic_bash(cmd)


@pytest.mark.parametrize("cmd", SAFE)
def test_ordinary_work_does_not_wake_the_owner(cmd: str) -> None:
    assert not is_catastrophic_bash(cmd)
