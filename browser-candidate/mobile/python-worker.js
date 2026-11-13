// SPDX-License-Identifier: Apache-2.0
import { loadPyodide } from '../pyodide/pyodide.mjs';

const read = async path => {
  const response = await fetch(new URL(path, import.meta.url));
  if (!response.ok) throw new Error(`Could not read ${path}`);
  return new Uint8Array(await response.arrayBuffer());
};

self.onmessage = async ({ data: { cells, inputs, csv } }) => {
  try {
    const python = await loadPyodide({
      indexURL: new URL('../pyodide/', import.meta.url).href,
    });
    await python.loadPackage('pandas');
    python.FS.mkdir('data');
    for (const name of inputs) python.FS.writeFile(name, await read(`../files/${name}`));
    const namespace = python.runPython('dict(__name__="__main__")');
    const formatResult = python.runPython(`
def waypoint_format(value):
    import json
    import pandas as pd

    if isinstance(value, pd.DataFrame):
        preview = value.iloc[:10, :12]
        rows = [["Missing" if pd.isna(item) else str(item) for item in row]
                for row in preview.itertuples(index=False, name=None)]
        table = {"headers": [str(column) for column in preview.columns],
                 "rows": rows, "totalRows": len(value), "totalColumns": len(value.columns)}
        return json.dumps({"text": repr(value), "table": table})
    return json.dumps({"text": repr(value)})
waypoint_format
`);
    let failed = false;
    for (const [position, [index, source]] of cells.entries()) {
      const outputs = [];
      let table;
      let streamBudget = 24000;
      function stream(name, line) {
        if (!streamBudget) return;
        if (outputs.at(-1)?.name !== name) outputs.push({ output_type: 'stream', name, text: '' });
        const output = outputs.at(-1);
        const chunk = (line + '\n').slice(0, streamBudget);
        streamBudget -= chunk.length;
        output.text += chunk;
        if (!streamBudget) output.text += '\n[Output limit reached]\n';
      }
      python.setStdout({ batched: line => stream('stdout', line) });
      python.setStderr({ batched: line => stream('stderr', line) });
      try {
        const value = await python.runPythonAsync(source, { globals: namespace });
        if (value !== undefined && value !== null) {
          try {
            const display = JSON.parse(formatResult(value));
            table = display.table;
            outputs.push({
              output_type: 'execute_result', execution_count: position + 1,
              data: { 'text/plain': display.text.slice(0, 24000) }, metadata: {},
            });
          } finally {
            if (value instanceof python.ffi.PyProxy) value.destroy();
          }
        }
      } catch (error) {
        failed = true;
        const traceback = String(error.message).slice(0, 24000).trimEnd().split('\n');
        const [name, ...message] = traceback.at(-1).split(':');
        outputs.push({ output_type: 'error', ename: name, evalue: message.join(':').trim(), traceback });
      }
      postMessage({ type: 'cell', index, outputs, table });
      if (failed) break;
    }
    let csvText = null;
    if (!failed && python.FS.analyzePath(csv).exists) {
      if (python.FS.stat(csv).size > 256 * 1024) throw new Error('The coursework CSV exceeds 256 KiB.');
      csvText = python.FS.readFile(csv, { encoding: 'utf8' });
    }
    postMessage({ type: 'complete', failed, csvText });
  } catch (error) {
    postMessage({ type: 'failed', message: error.message });
  }
};
