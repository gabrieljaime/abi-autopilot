# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python autopilot.py daemon --once
