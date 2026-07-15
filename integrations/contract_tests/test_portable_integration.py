from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "tts_more"


def _git_tracked_paths(root: Path, expected: set[str] | dict[str, object]) -> set[str]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.quotePath=false",
            "ls-files",
            "--",
            *sorted(expected),
        ],
        check=True,
        capture_output=True,
    )
    return set(completed.stdout.decode("utf-8", errors="strict").splitlines())


def _active_cmd_lines(path: Path) -> list[str]:
    active: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if not line or lowered.startswith("rem ") or lowered.startswith("::"):
            continue
        if lowered in {"@echo off", "setlocal", "setlocal enableextensions"}:
            continue
        active.append(line)
    return active


_POWERSHELL_SEMANTIC_CONTRACT = r"""
$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Assert-Contract {
    param([bool]$Condition, [string]$Message)
    if (!$Condition) { throw $Message }
}

function Parse-ContractAst {
    param([string]$Path)
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    Assert-Contract ($errors.Count -eq 0) ("PowerShell parse failed: " + (($errors | ForEach-Object Message) -join "; "))
    return $ast
}

function Get-ContractFunction {
    param($Ast, [string]$Name)
    $functions = @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | Where-Object { $_.Name -ceq $Name })
    Assert-Contract ($functions.Count -eq 1) ("expected one function: " + $Name)
    return $functions[0]
}

function Get-ContractAssignments {
    param($Ast, [string]$Variable)
    return @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true) | Where-Object {
        $_.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and $_.Left.VariablePath.UserPath -ceq $Variable
    })
}

function Get-ContractCommands {
    param($Ast, [string]$Name)
    return @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true) | Where-Object { $_.GetCommandName() -ceq $Name })
}

function Get-ContractParameterArgument {
    param($Command, [string]$Name)
    $elements = @($Command.CommandElements)
    for ($index = 1; $index -lt $elements.Count; $index++) {
        $element = $elements[$index]
        if ($element -is [System.Management.Automation.Language.CommandParameterAst] -and $element.ParameterName -ceq $Name) {
            Assert-Contract ($index + 1 -lt $elements.Count) ("missing argument for parameter: " + $Name)
            return $elements[$index + 1]
        }
    }
    return $null
}

function Get-ContractMemberPath {
    param($Node)
    if ($Node -is [System.Management.Automation.Language.VariableExpressionAst]) {
        return $Node.VariablePath.UserPath
    }
    if ($Node -is [System.Management.Automation.Language.MemberExpressionAst]) {
        $prefix = Get-ContractMemberPath $Node.Expression
        if ([string]::IsNullOrWhiteSpace($prefix)) { return "" }
        return $prefix + "." + [string]$Node.Member.Value
    }
    return ""
}

function Test-ContractContainsMemberPath {
    param($Ast, [string]$Expected)
    foreach ($member in @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.MemberExpressionAst] }, $true))) {
        if ((Get-ContractMemberPath $member) -ceq $Expected) { return $true }
    }
    return $false
}

function Test-ContractVariable {
    param($Node, [string]$Expected)
    return $Node -is [System.Management.Automation.Language.VariableExpressionAst] -and $Node.VariablePath.UserPath -ceq $Expected
}

function Test-ContractString {
    param($Node, [string]$Expected)
    return $Node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and [string]$Node.Value -ceq $Expected
}

function Test-ContractJoinPath {
    param($Ast, [string]$RootVariable, [string]$RelativePath)
    foreach ($command in @(Get-ContractCommands $Ast "Join-Path")) {
        $elements = @($command.CommandElements)
        if ($elements.Count -eq 3 -and (Test-ContractVariable $elements[1] $RootVariable) -and (Test-ContractString $elements[2] $RelativePath)) {
            return $true
        }
    }
    return $false
}

function Test-ContractJoinPathMember {
    param($Ast, [string]$RootMember, [string]$RelativePath)
    foreach ($command in @(Get-ContractCommands $Ast "Join-Path")) {
        $elements = @($command.CommandElements)
        if ($elements.Count -eq 3 -and (Get-ContractMemberPath $elements[1]) -ceq $RootMember -and (Test-ContractString $elements[2] $RelativePath)) {
            return $true
        }
    }
    return $false
}

function Test-ContractJoinPathMemberVariable {
    param($Ast, [string]$RootMember, [string]$ChildVariable)
    foreach ($command in @(Get-ContractCommands $Ast "Join-Path")) {
        $elements = @($command.CommandElements)
        if ($elements.Count -eq 3 -and (Get-ContractMemberPath $elements[1]) -ceq $RootMember -and (Test-ContractVariable $elements[2] $ChildVariable)) {
            return $true
        }
    }
    return $false
}

function Test-ContractDescendantOf {
    param($Node, $Ancestor)
    $current = $Node
    while ($null -ne $current) {
        if ([object]::ReferenceEquals($current, $Ancestor)) { return $true }
        $current = $current.Parent
    }
    return $false
}

function Test-ContractSchemaV2Ancestor {
    param($Node)
    $tryBody = $Node.Parent
    if ($tryBody -isnot [System.Management.Automation.Language.StatementBlockAst]) { return $false }
    $tryStatement = $tryBody.Parent
    if ($tryStatement -isnot [System.Management.Automation.Language.TryStatementAst] -or ![object]::ReferenceEquals($tryStatement.Body, $tryBody)) { return $false }
    $clauseBody = $tryStatement.Parent
    if ($clauseBody -isnot [System.Management.Automation.Language.StatementBlockAst]) { return $false }
    $ifStatement = $clauseBody.Parent
    if ($ifStatement -isnot [System.Management.Automation.Language.IfStatementAst]) { return $false }
    foreach ($clause in $ifStatement.Clauses) {
        $condition = $clause.Item1
        $hasSchema = Test-ContractContainsMemberPath $condition "manifest.schema_version"
        $hasTwo = @($condition.FindAll({ param($candidate) $candidate -is [System.Management.Automation.Language.ConstantExpressionAst] -and $candidate.Value -eq 2 }, $true)).Count -gt 0
        $hasEquality = @($condition.FindAll({ param($candidate) $candidate -is [System.Management.Automation.Language.BinaryExpressionAst] -and $candidate.Operator.ToString() -match "Eq$" }, $true)).Count -gt 0
        if ($hasSchema -and $hasTwo -and $hasEquality -and [object]::ReferenceEquals($clause.Item2, $clauseBody)) { return $true }
    }
    return $false
}

function Test-ContractVariableEqualsString {
    param($Ast, [string]$Variable, [string]$Value)
    $binaries = @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.BinaryExpressionAst] }, $true))
    if ($binaries.Count -ne 1) { return $false }
    $binary = $binaries[0]
    return $binary.Operator.ToString() -match "Eq$" -and (Test-ContractVariable $binary.Left $Variable) -and (Test-ContractString $binary.Right $Value)
}

function Test-ContractTopLevelAssignment {
    param($Assignment)
    return $Assignment.Parent -is [System.Management.Automation.Language.NamedBlockAst] -and $Assignment.Parent.Parent -is [System.Management.Automation.Language.ScriptBlockAst]
}

function Test-ContractDirectCommandInTry {
    param($Command, $TryStatement)
    if ($TryStatement -isnot [System.Management.Automation.Language.TryStatementAst]) { return $false }
    $current = $Command.Parent
    while ($null -ne $current -and ![object]::ReferenceEquals($current, $TryStatement.Body)) {
        if ($current -is [System.Management.Automation.Language.IfStatementAst] -or
            $current -is [System.Management.Automation.Language.SwitchStatementAst] -or
            $current -is [System.Management.Automation.Language.LoopStatementAst] -or
            $current -is [System.Management.Automation.Language.TrapStatementAst] -or
            $current -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
            $current -is [System.Management.Automation.Language.TryStatementAst]) { return $false }
        $current = $current.Parent
    }
    return [object]::ReferenceEquals($current, $TryStatement.Body)
}

function Test-ContractInvokeMemberWithMemberArgument {
    param($Ast, [string]$Method, [string]$MemberPath)
    $matches = @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true) | Where-Object {
        [string]$_.Member.Value -ceq $Method -and @($_.Arguments | Where-Object { (Get-ContractMemberPath $_) -ceq $MemberPath }).Count -eq 1
    })
    return $matches.Count -eq 1
}

function Test-ContractHashtableVariable {
    param($Ast, [string]$Key, [string]$Variable)
    $matches = 0
    foreach ($table in @($Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.HashtableAst] }, $true))) {
        $returned = $false
        $parent = $table.Parent
        while ($null -ne $parent) {
            if ($parent -is [System.Management.Automation.Language.ReturnStatementAst]) { $returned = $true; break }
            $parent = $parent.Parent
        }
        if (!$returned) { continue }
        foreach ($pair in $table.KeyValuePairs) {
            if (Test-ContractString $pair.Item1 $Key) {
                $variables = @($pair.Item2.FindAll({ param($node) $node -is [System.Management.Automation.Language.VariableExpressionAst] -and $node.VariablePath.UserPath -ceq $Variable }, $true))
                if ($variables.Count -gt 0) { $matches++ }
            }
        }
    }
    return $matches -eq 1
}

function Test-ContractCommandOutsideFunction {
    param($Command)
    $parent = $Command.Parent
    while ($null -ne $parent) {
        if ($parent -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $false }
        $parent = $parent.Parent
    }
    return $true
}

$controller = Parse-ContractAst $env:TTS_MORE_CONTRACT_CONTROLLER
$worker = Parse-ContractAst $env:TTS_MORE_CONTRACT_WORKER
$contextFunction = Get-ContractFunction $controller "Get-PackageContext"
$serviceFunction = Get-ContractFunction $controller "Invoke-ServiceStart"
$lockFunction = Get-ContractFunction $controller "Open-PackageOperationLock"
$initializeOperationFunction = Get-ContractFunction $controller "Initialize-Operation"
$mainTries = @($controller.FindAll({ param($node) $node -is [System.Management.Automation.Language.TryStatementAst] }, $true) | Where-Object { [object]::ReferenceEquals($_.Parent, $controller.EndBlock) })
Assert-Contract ($mainTries.Count -eq 1) "controller must have exactly one direct main try statement"
$mainTry = $mainTries[0]

$operationsMatches = @()
$v2OperationsAssignments = @()
foreach ($assignment in @(Get-ContractAssignments $contextFunction.Body "operationsRoot")) {
    if (Test-ContractSchemaV2Ancestor $assignment) { $v2OperationsAssignments += $assignment }
    foreach ($command in @(Get-ContractCommands $assignment.Right "Resolve-PortablePackagePath")) {
        $relative = Get-ContractParameterArgument $command "RelativePath"
        $label = Get-ContractParameterArgument $command "Label"
        if ($null -ne $relative -and $null -ne $label -and [object]::ReferenceEquals($command.Parent, $assignment.Right) -and (Test-ContractContainsMemberPath $relative "manifest.data.operations") -and (Test-ContractString $label "data.operations") -and (Test-ContractSchemaV2Ancestor $assignment)) {
            $operationsMatches += $assignment
        }
    }
}
Assert-Contract ($v2OperationsAssignments.Count -eq 1) ("schema-v2 branch must assign operationsRoot exactly once; found " + $v2OperationsAssignments.Count)
Assert-Contract ($operationsMatches.Count -eq 1) "schema-v2 data.operations is not the active operations root assignment"
Assert-Contract (Test-ContractHashtableVariable $contextFunction.Body "OperationsRoot" "operationsRoot") "Get-PackageContext does not return the resolved operationsRoot"
$lockAssignments = @(Get-ContractAssignments $lockFunction.Body "lockPath")
Assert-Contract ($lockAssignments.Count -eq 1 -and (Test-ContractJoinPathMember $lockAssignments[0].Right "context.OperationsRoot" ".start.lock")) "operation lock does not consume context.OperationsRoot"

$serviceAssignments = @(Get-ContractAssignments $contextFunction.Body "serviceScript")
Assert-Contract ($serviceAssignments.Count -eq 1) "serviceScript must have exactly one active assignment"
Assert-Contract ($serviceAssignments[0].Right -is [System.Management.Automation.Language.IfStatementAst]) "serviceScript assignment must select by component"
$serviceSelector = $serviceAssignments[0].Right
Assert-Contract ($serviceSelector.Clauses.Count -eq 1 -and (Test-ContractVariableEqualsString $serviceSelector.Clauses[0].Item1 "component" "tts-more")) "serviceScript selector must test component == tts-more"
Assert-Contract (Test-ContractJoinPath $serviceSelector.Clauses[0].Item2 "resolvedRoot" "scripts\start-production.ps1") "TTS More serviceScript does not select scripts/start-production.ps1"
Assert-Contract (Test-ContractJoinPath $serviceSelector.ElseClause "bundle" "Start-Worker.ps1") "fork serviceScript does not select the controlled Start-Worker.ps1"

$delegates = @(Get-ContractCommands $serviceFunction.Body "Invoke-ChildPowerShell")
Assert-Contract ($delegates.Count -eq 1) "Invoke-ServiceStart must delegate exactly once"
$delegateAssignments = @(Get-ContractAssignments $serviceFunction.Body "result")
Assert-Contract ($delegateAssignments.Count -eq 1 -and [object]::ReferenceEquals($delegateAssignments[0].Parent, $serviceFunction.Body.EndBlock)) "Invoke-ServiceStart delegate assignment is not a direct function statement"
Assert-Contract ($delegateAssignments[0].Right -is [System.Management.Automation.Language.PipelineAst] -and [object]::ReferenceEquals($delegates[0].Parent, $delegateAssignments[0].Right)) "Invoke-ServiceStart delegate is not the direct result pipeline"
$delegateScript = Get-ContractParameterArgument $delegates[0] "Script"
$delegateArguments = Get-ContractParameterArgument $delegates[0] "Arguments"
Assert-Contract ((Get-ContractMemberPath $delegateScript) -ceq "context.ServiceScript") "Invoke-ServiceStart does not pass context.ServiceScript"
Assert-Contract (Test-ContractVariable $delegateArguments "arguments") "Invoke-ServiceStart does not pass the worker arguments array"
$serviceCalls = @(Get-ContractCommands $mainTry.Body "Invoke-ServiceStart")
Assert-Contract ($serviceCalls.Count -eq 1 -and $serviceCalls[0].Parent -is [System.Management.Automation.Language.PipelineAst] -and [object]::ReferenceEquals($serviceCalls[0].Parent.Parent, $mainTry.Body)) "the controller main flow does not call Invoke-ServiceStart as a direct try statement"
Assert-Contract (Test-ContractVariable (Get-ContractParameterArgument $serviceCalls[0] "Root") "root") "main service call does not pass root"
Assert-Contract (Test-ContractVariable (Get-ContractParameterArgument $serviceCalls[0] "Operation") "operation") "main service call does not pass operation"
Assert-Contract (Test-ContractVariable (Get-ContractParameterArgument $serviceCalls[0] "PortOverride") "PortOverride") "main service call does not pass PortOverride"

$operationRootAssignments = @(Get-ContractAssignments $initializeOperationFunction.Body "operationRoot")
Assert-Contract ($operationRootAssignments.Count -eq 1 -and [object]::ReferenceEquals($operationRootAssignments[0].Parent, $initializeOperationFunction.Body.EndBlock)) "Initialize-Operation operationRoot is not one direct assignment"
Assert-Contract (Test-ContractJoinPathMemberVariable $operationRootAssignments[0].Right "context.OperationsRoot" "canonicalId") "operationRoot is not derived from context.OperationsRoot/canonicalId"

$boundaryIfs = @($initializeOperationFunction.Body.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.IfStatementAst] -and $_.Clauses.Count -eq 1 -and @(Get-ContractCommands $_.Clauses[0].Item1 "Test-PathWithinRoot").Count -eq 1
})
Assert-Contract ($boundaryIfs.Count -eq 1) "Initialize-Operation boundary check is not one direct if statement"
$boundaryCondition = $boundaryIfs[0].Clauses[0].Item1
$pathChecks = @(Get-ContractCommands $boundaryCondition "Test-PathWithinRoot")
Assert-Contract ($pathChecks.Count -eq 1) "Initialize-Operation must call Test-PathWithinRoot exactly once"
Assert-Contract ((Get-ContractMemberPath (Get-ContractParameterArgument $pathChecks[0] "Root")) -ceq "context.OperationsRoot") "Test-PathWithinRoot does not use context.OperationsRoot"
Assert-Contract (Test-ContractVariable (Get-ContractParameterArgument $pathChecks[0] "Path") "operationRoot") "Test-PathWithinRoot does not validate operationRoot"
$orExpressions = @($boundaryCondition.FindAll({ param($node) $node -is [System.Management.Automation.Language.BinaryExpressionAst] -and $node.Operator.ToString() -match "Or$" }, $true))
Assert-Contract ($orExpressions.Count -eq 1) "Initialize-Operation boundary condition must combine containment and parent equality"
$equalsCalls = @($boundaryCondition.FindAll({ param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and [string]$node.Member.Value -ceq "Equals" }, $true))
Assert-Contract ($equalsCalls.Count -eq 1 -and $equalsCalls[0].Static) "Initialize-Operation parent equality is missing"
$parentSplits = @(Get-ContractCommands $equalsCalls[0] "Split-Path")
Assert-Contract ($parentSplits.Count -eq 1 -and (Test-ContractVariable (Get-ContractParameterArgument $parentSplits[0] "Parent") "operationRoot")) "parent equality does not inspect operationRoot parent"
Assert-Contract (Test-ContractInvokeMemberWithMemberArgument $equalsCalls[0] "GetFullPath" "context.OperationsRoot") "parent equality does not normalize context.OperationsRoot"

$operationContractTries = @($initializeOperationFunction.Body.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.TryStatementAst] -and @(Get-ContractCommands $_.Body "Assert-PortableExactOperationContract").Count -eq 1
})
Assert-Contract ($operationContractTries.Count -eq 1) "Initialize-Operation operation contract is not one direct try statement"
$operationContractCommands = @(Get-ContractCommands $operationContractTries[0].Body "Assert-PortableExactOperationContract")
Assert-Contract ($operationContractCommands.Count -eq 1 -and (Test-ContractDirectCommandInTry $operationContractCommands[0] $operationContractTries[0])) "operation contract call is nested or unreachable"
Assert-Contract ((Get-ContractMemberPath (Get-ContractParameterArgument $operationContractCommands[0] "OperationsRoot")) -ceq "context.OperationsRoot") "operation contract does not use context.OperationsRoot"
Assert-Contract (Test-ContractVariable (Get-ContractParameterArgument $operationContractCommands[0] "OperationRoot") "operationRoot") "operation contract does not use operationRoot"

$mainActivePathAssignments = @()
foreach ($assignment in @(Get-ContractAssignments $controller "activePath")) {
    if ($assignment.Right -is [System.Management.Automation.Language.PipelineAst] -and
        [object]::ReferenceEquals($assignment.Parent, $mainTry.Body) -and
        (Test-ContractJoinPathMember $assignment.Right "script:Context.OperationsRoot" "active-start.json")) {
        $mainActivePathAssignments += $assignment
    }
}
Assert-Contract ($mainActivePathAssignments.Count -eq 1) "main activePath is not one direct context.OperationsRoot assignment"

$pythonAssignments = @(Get-ContractAssignments $worker "Python")
Assert-Contract ($pythonAssignments.Count -eq 1) "worker Python must have exactly one active assignment"
Assert-Contract (Test-ContractTopLevelAssignment $pythonAssignments[0]) "worker Python assignment is not an active top-level statement"
Assert-Contract ($pythonAssignments[0].Right -is [System.Management.Automation.Language.PipelineAst]) "worker Python assignment is not a direct executable pipeline"
Assert-Contract (Test-ContractJoinPath $pythonAssignments[0].Right "Root" "runtime\live\python.exe") "worker Python is not package-private runtime/live/python.exe"

$argumentAssignments = @(Get-ContractAssignments $worker "arguments")
Assert-Contract ($argumentAssignments.Count -eq 1) "worker arguments must have exactly one active assignment"
Assert-Contract (Test-ContractTopLevelAssignment $argumentAssignments[0]) "worker arguments assignment is not an active top-level statement"
Assert-Contract ($argumentAssignments[0].Right -is [System.Management.Automation.Language.CommandExpressionAst]) "worker arguments assignment is not a direct array expression"
$argumentStrings = @($argumentAssignments[0].Right.FindAll({ param($node) $node -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | ForEach-Object { [string]$_.Value })
Assert-Contract ($argumentStrings -ccontains "-m" -and $argumentStrings -ccontains "uvicorn") "worker arguments do not execute uvicorn as a module"
Assert-Contract (Test-ContractContainsMemberPath $argumentAssignments[0].Right "config.module") "worker arguments do not use the configured worker module"

$processAssignments = @(Get-ContractAssignments $worker "process")
Assert-Contract ($processAssignments.Count -eq 1 -and (Test-ContractTopLevelAssignment $processAssignments[0])) "worker process assignment is not an active top-level statement"
Assert-Contract ($processAssignments[0].Right -is [System.Management.Automation.Language.PipelineAst]) "worker process assignment is not a direct executable pipeline"
$startProcesses = @(Get-ContractCommands $processAssignments[0].Right "Start-Process")
Assert-Contract ($startProcesses.Count -eq 1) "worker must start exactly one service process from the top-level process assignment"
$filePath = Get-ContractParameterArgument $startProcesses[0] "FilePath"
$argumentList = Get-ContractParameterArgument $startProcesses[0] "ArgumentList"
Assert-Contract (Test-ContractVariable $filePath "Python") "worker service process does not use package-private Python"
Assert-Contract (Test-ContractVariable $argumentList "arguments") "worker service process does not use the uvicorn arguments"

$forbiddenParameters = @($worker.FindAll({ param($node) $node -is [System.Management.Automation.Language.ParameterAst] -and $node.Name.VariablePath.UserPath -match "(?i)python" }, $true))
$forbiddenOverrides = @($worker.FindAll({ param($node) $node -is [System.Management.Automation.Language.VariableExpressionAst] -and $node.VariablePath.UserPath -ceq "env:TTS_MORE_PYTHON_EXE" }, $true))
$forbiddenStrings = @($worker.FindAll({ param($node) $node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and ([string]$node.Value -match "(?i)\.venv" -or ($node.Parent -isnot [System.Management.Automation.Language.MemberExpressionAst] -and [string]$node.Value -in @("python", "python.exe", "py", "py.exe"))) }, $true))
Assert-Contract ($forbiddenParameters.Count -eq 0) "worker accepts a Python override parameter"
Assert-Contract ($forbiddenOverrides.Count -eq 0) "worker accepts TTS_MORE_PYTHON_EXE"
Assert-Contract ($forbiddenStrings.Count -eq 0) "worker contains a system/.venv Python fallback"

Write-Output "PORTABLE_CONTROL_FLOW_AST_OK"
"""


def _powershell_executable(platform_name: str | None = None) -> str:
    platform_name = platform_name or os.name
    system_root = os.environ.get("SystemRoot")
    if platform_name == "nt" and system_root:
        windows_powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if windows_powershell.is_file():
            return str(windows_powershell)
    candidates = ("powershell.exe", "pwsh") if platform_name == "nt" else ("pwsh",)
    for name in candidates:
        executable = shutil.which(name)
        if executable:
            return executable
    raise AssertionError("PowerShell 5.1 or PowerShell 7 is required for the portable integration contract")


def _verify_powershell_control_flow(bundle: Path) -> None:
    environment = os.environ.copy()
    environment["TTS_MORE_CONTRACT_CONTROLLER"] = str(bundle / "Invoke-PortableStart.ps1")
    environment["TTS_MORE_CONTRACT_WORKER"] = str(bundle / "Start-Worker.ps1")
    with tempfile.TemporaryDirectory(prefix="tts-more-contract-") as directory:
        verifier = Path(directory) / "verify-portable-control-flow.ps1"
        verifier.write_text(_POWERSHELL_SEMANTIC_CONTRACT, encoding="utf-8-sig")
        completed = subprocess.run(
            [
                _powershell_executable(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(verifier),
            ],
            env=environment,
            capture_output=True,
            check=False,
        )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        raise AssertionError(f"PowerShell control-flow contract failed:\n{stdout}\n{stderr}")
    if "PORTABLE_CONTROL_FLOW_AST_OK" not in stdout:
        raise AssertionError(f"PowerShell control-flow contract returned no success marker:\n{stdout}\n{stderr}")


class PortableIntegrationContractTests(unittest.TestCase):
    def test_powershell_resolver_prefers_windows_51_and_falls_back_to_pwsh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            system_root = Path(directory)
            windows_powershell = (
                system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            windows_powershell.parent.mkdir(parents=True)
            windows_powershell.touch()
            with mock.patch.dict(os.environ, {"SystemRoot": str(system_root)}, clear=True):
                with mock.patch("shutil.which", return_value="C:/Tools/pwsh.exe"):
                    self.assertEqual(str(windows_powershell), _powershell_executable("nt"))

        def find_pwsh(name: str) -> str | None:
            return "/usr/bin/pwsh" if name == "pwsh" else None

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", side_effect=find_pwsh):
                self.assertEqual("/usr/bin/pwsh", _powershell_executable("posix"))
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaisesRegex(AssertionError, "PowerShell 5.1 or PowerShell 7"):
                    _powershell_executable("posix")

    def test_git_tracked_paths_decodes_utf8_without_locale_dependency(self) -> None:
        expected = {"Start.cmd", "使用说明-先看这里.txt"}
        completed = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="Start.cmd\n使用说明-先看这里.txt\n".encode("utf-8"),
            stderr=b"",
        )

        with mock.patch("subprocess.run", return_value=completed) as run:
            tracked = _git_tracked_paths(ROOT, expected)

        self.assertEqual(expected, tracked)
        self.assertIn("core.quotePath=false", run.call_args.args[0])
        self.assertNotIn("text", run.call_args.kwargs)
        self.assertNotIn("encoding", run.call_args.kwargs)

    def test_controlled_mirror_has_no_hash_drift(self) -> None:
        manifest = json.loads((BUNDLE / "integration.manifest.json").read_text(encoding="utf-8"))
        expected = manifest["files"]
        for relative, digest in expected.items():
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            canonical = path.read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), digest, relative)
        controlled = {
            path.relative_to(ROOT).as_posix()
            for path in BUNDLE.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.name != "integration.manifest.json"
        }
        self.assertEqual(controlled, {name for name in expected if name.startswith("tts_more/")})
        tracked = _git_tracked_paths(ROOT, expected)
        self.assertEqual(set(expected), tracked, "controlled integration files must be Git tracked")

    def test_package_entrypoints_and_native_webui_are_separate(self) -> None:
        for name in (
            "Initialize.cmd",
            "Start.cmd",
            "Stop.cmd",
            "Repair.cmd",
            "Build-Package.ps1",
            "Start-WebUI.cmd",
            "使用说明-先看这里.txt",
        ):
            self.assertTrue((ROOT / name).is_file(), name)
        start_path = ROOT / "Start.cmd"
        start = start_path.read_text(encoding="utf-8")
        webui = (ROOT / "Start-WebUI.cmd").read_text(encoding="utf-8")
        self.assertEqual(
            [
                'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
                '"%~dp0tts_more\\Invoke-PortableStart.ps1" %*',
                "exit /b %errorlevel%",
            ],
            _active_cmd_lines(start_path),
        )
        self.assertNotEqual(start, webui)

    def test_controller_uses_manifest_operations_worker_delegate_and_private_runtime(self) -> None:
        _verify_powershell_control_flow(BUNDLE)

    def test_operation_protocol_is_controlled(self) -> None:
        self.assertTrue((BUNDLE / "portable_operations.py").is_file(), "portable operation protocol")

    def test_model_and_device_locks_are_complete_and_immutable(self) -> None:
        model_lock = json.loads((BUNDLE / "locks" / "models.lock.json").read_text(encoding="utf-8"))
        self.assertTrue(model_lock["complete"], model_lock["missing_required_paths"])
        targets = {asset["target"] for asset in model_lock["assets"]}
        self.assertTrue(set(model_lock["required_paths"]) <= targets)
        for asset in model_lock["assets"]:
            self.assertRegex(asset["source_revision"], r"^[0-9a-f]{40}$")
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(asset["size_bytes"], 0)
            self.assertTrue(all(asset["source_revision"] in url for url in asset["urls"]))
        for profile in ("cpu", "cu126", "cu128"):
            contents = (BUNDLE / "locks" / f"requirements-{profile}.lock.txt").read_text(encoding="utf-8")
            starts = list(re.finditer(r"(?m)^[A-Za-z0-9_.-]+==[^\s\\]+", contents))
            self.assertTrue(starts, profile)
            for index, start in enumerate(starts):
                end = starts[index + 1].start() if index + 1 < len(starts) else len(contents)
                self.assertIn("--hash=sha256:", contents[start.start():end], start.group(0))

    def test_full_release_is_fail_closed_in_github_actions(self) -> None:
        builder = (BUNDLE / "Build-Package.ps1").read_text(encoding="utf-8")
        self.assertIn('$env:GITHUB_ACTIONS -eq "true"', builder)
        self.assertIn("audit-release --zip", builder)


if __name__ == "__main__":
    unittest.main()
