// @macro:retained
//
// Registers the pythonVenvSelect.select command invoked by tasks.json ("Project: Reselect Python
// Virtual Environment") after bootstrap has run. It resolves ${workspaceFolder}/.venv and selects
// it as the workspace's Python environment through the Python Environments extension API. Nothing
// runs unless the command is invoked: selection happens only when a task says so.
//
// Why this exists: ms-python.vscode-python-envs runs interpreter selection once, at its own
// startup. On the first open of a fresh clone the venv does not exist yet at that point, and no
// built-in command re-runs selection non-interactively (python-envs.set and friends always end in
// an interactive picker); the non-interactive setter is only reachable through the extension's
// exports, which a task cannot touch. This macro is the bridge from the command layer to that API.
//
// The command returns a string in every code path (the selected interpreter path, or a reason for
// skipping): a command-type task input that resolves undefined is treated as a user cancellation
// and cancels the task with a "Cancelled" notification.

__disposables.push(
    vscode.commands.registerCommand('pythonVenvSelect.select', selectVenv),
);

async function selectVenv() {
    const folder = vscode.workspace.workspaceFolders?.[0];
    if (!folder) {
        return 'skipped: no workspace folder';
    }
    // The macro sandbox does not expose Node globals such as `process`, so
    // instead of branching on process.platform probe both interpreter layouts
    // (POSIX and Windows) and use whichever exists.
    let python;
    for (const layout of ['.venv/bin/python', '.venv/Scripts/python.exe']) {
        const candidate = vscode.Uri.joinPath(folder.uri, layout);
        try {
            await vscode.workspace.fs.stat(candidate);
            python = candidate;
            break;
        } catch {
            // keep probing
        }
    }
    if (!python) {
        return 'skipped: no venv on disk';
    }
    const extension = vscode.extensions.getExtension('ms-python.vscode-python-envs');
    if (!extension) {
        return 'skipped: python-envs extension not installed';
    }
    const api = await extension.activate();
    if (!api) {
        return 'skipped: python-envs API not available';
    }
    const env = await api.resolveEnvironment(python);
    if (!env) {
        return 'skipped: venv did not resolve to an environment';
    }
    const current = await api.getEnvironment(folder.uri);
    if (current?.envId.id !== env.envId.id) {
        await api.setEnvironment(folder.uri, env);
    }
    return python.fsPath;
}
