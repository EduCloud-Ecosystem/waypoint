// SPDX-License-Identifier: Apache-2.0
const byId = id => document.getElementById(id);
const announce = message => { byId('status').textContent = message; };
const exportStatus = message => { byId('download-status').textContent = message; };
const resultCount = new Map();

function append(parent, tag, text) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  parent.append(element);
  return element;
}

async function read(url, method) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not open ${url}`);
  return response[method]();
}

function editor(parent, label, value, rows, code = false) {
  const field = append(append(parent, 'label', label), 'textarea');
  field.rows = rows;
  field.value = value;
  if (code) {
    field.spellcheck = false;
    field.setAttribute('autocorrect', 'off');
    field.setAttribute('autocapitalize', 'none');
  }
  return field;
}

function renderTable(output, table) {
  const region = append(output, 'div');
  region.className = 'table-region';
  region.tabIndex = 0;
  region.setAttribute('role', 'region');
  region.setAttribute('aria-label', 'Scrollable table preview');
  const grid = append(region, 'table');
  append(grid, 'caption', `Showing ${table.rows.length} of ${table.totalRows} rows and ${table.headers.length} of ${table.totalColumns} columns`);
  const headers = append(append(grid, 'thead'), 'tr');
  for (const name of table.headers) append(headers, 'th', name).scope = 'col';
  const body = append(grid, 'tbody');
  for (const row of table.rows) {
    const line = append(body, 'tr');
    for (const item of row) append(line, 'td', item);
  }
}

function renderOutput(target, result) {
  target.replaceChildren();
  append(target, 'h4', 'Output');
  if (!result?.outputs.length) {
    append(target, 'p', result ? 'This cell ran without output.' : 'Run the notebook to see output here.');
    return;
  }
  for (const output of result.outputs) {
    if (output.output_type === 'execute_result' && result.table) {
      renderTable(target, result.table);
    } else {
      const text = output.text ?? output.data?.['text/plain'] ?? output.traceback.join('\n');
      append(target, 'pre', text);
    }
  }
}

function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/octet-stream' }));
  const link = append(document.body, 'a');
  link.href = url;
  link.download = name;
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  exportStatus(`Download requested for ${name}. Check your downloads.`);
}

async function open() {
  const manifest = await read('../files/bundle-manifest.json', 'json');
  const [name, csvName, ...notes] = manifest.submission_files;
  const starter = await read(`../files/${name}`, 'json');
  const noteDefaults = await Promise.all(notes.map(note => read(`../files/${note}`, 'text')));
  const fallback = new URL(manifest.hosted_fallback.origin);
  if (fallback.protocol === 'https:' && !fallback.hostname.endsWith('.invalid')) {
    const link = document.createElement('a');
    link.href = fallback.origin;
    link.textContent = 'Hosted fallback';
    byId('fallback').replaceWith(link);
  } else {
    byId('fallback').textContent = 'Hosted fallback: ask your course operator';
  }

  const key = `waypoint/mobile${location.pathname}:${manifest.files.find(file => file.path === name).sha256}`;
  let draft;
  let openMessage = 'Notebook ready. Python loads when you run it.';
  try {
    draft = JSON.parse(localStorage.getItem(key));
    if (draft) openMessage = 'Saved work restored on this device.';
  } catch {
    openMessage = 'Local save could not be opened. You can still download copies of your work.';
  }
  const notebook = structuredClone(starter);
  const inputs = manifest.files.map(file => file.path).filter(path => path.startsWith('data/'));
  let worker;
  const fields = [];
  const codeCells = [];
  const headings = ['About the assignment', 'Predict the schemas', 'Describe the rows',
    'Load the source data', 'Wrangling instructions', 'Write the solution', 'About this fixture'];
  let csv = draft?.csv ?? null;

  function clearResults() {
    resultCount.clear();
    csv = null;
    for (const { index, output } of codeCells) {
      notebook.cells[index].execution_count = null;
      notebook.cells[index].outputs = [];
      renderOutput(output);
    }
    csvButton.disabled = true;
  }

  notebook.cells.forEach((cell, index) => {
    const saved = draft?.notebook?.cells?.[index];
    if (saved) Object.assign(cell, saved);
    const article = append(byId('notebook'), 'article');
    const heading = append(article, 'h3', headings[index]);
    heading.id = `cell-${index + 1}`;
    heading.tabIndex = -1;
    article.setAttribute('aria-labelledby', heading.id);
    cell.source = [cell.source].flat().join('');
    const source = cell.source;
    const code = cell.cell_type === 'code';
    if (!code && ![1, 2].includes(index)) {
      append(article, 'p', source).className = 'instruction';
      return;
    }
    const prompt = code ? (index === 3 ? 'Data loading code' : 'Solution code') : 'Your prediction (Markdown)';
    if (!code) append(article, 'p', [starter.cells[index].source].flat().join('').replace(/^REPLACE_ME: /, ''));
    const field = editor(article, prompt, source, code ? (index === 3 ? 7 : 10) : 3, code);
    fields.push(field);
    field.addEventListener('input', () => {
      cell.source = field.value;
      if (code && (resultCount.size || csv !== null)) clearResults();
    });
    if (code) {
      const output = append(article, 'div');
      output.className = 'output';
      codeCells.push({ index, output });
      if (index === 5) {
        const link = append(article, 'a', 'Go to Run all cells');
        link.href = '#run';
        link.className = 'jump-action';
      }
      const result = draft?.results?.[index];
      if (result) resultCount.set(index, result);
      renderOutput(output, result);
    }
  });

  const files = byId('files');
  const notebookFile = append(files, 'div');
  notebookFile.className = 'file';
  append(notebookFile, 'button', 'Download working notebook').addEventListener('click', () => {
    download(name, JSON.stringify(notebook, null, 2) + '\n');
  });
  const csvButton = append(notebookFile, 'button', `Download ${csvName}`);
  csvButton.disabled = csv === null;
  csvButton.addEventListener('click', () => download(csvName, csv));
  const noteFields = notes.map((note, index) => {
    const card = append(files, 'div');
    card.className = 'file';
    const field = editor(card, note, draft?.notes?.[index] ?? noteDefaults[index], 5);
    fields.push(field);
    append(card, 'button', `Download ${note}`).addEventListener('click', () => download(note, field.value));
    return field;
  });

  function busy(running) {
    for (const field of fields) field.readOnly = running;
    for (const button of files.querySelectorAll('button')) button.disabled = running;
    csvButton.disabled = running || csv === null;
    byId('run').disabled = running;
    byId('save').disabled = running;
    byId('stop').hidden = !running;
  }

  function finish(message) {
    worker?.terminate();
    worker = undefined;
    if (document.activeElement === byId('stop')) {
      byId('run').disabled = false;
      byId('run').focus();
    }
    busy(false);
    announce(message);
  }

  byId('run').addEventListener('click', () => {
    clearResults();
    busy(true);
    announce('Running code cells. Python and pandas may take a moment to load.');
    try {
      worker = new Worker(new URL('./python-worker.js', import.meta.url), { type: 'module' });
    } catch {
      finish('Python could not start. Try reloading the page or use the hosted fallback.');
      return;
    }
    worker.onmessage = ({ data }) => {
      if (data.type === 'cell') {
        const cell = notebook.cells[data.index];
        cell.execution_count = resultCount.size + 1;
        cell.outputs = data.outputs;
        resultCount.set(data.index, data);
        renderOutput(codeCells.find(item => item.index === data.index).output, data);
      } else if (data.type === 'complete') {
        csv = data.csvText;
        const message = data.failed ? 'Run stopped at a Python error. Read the cell output.'
          : csv === null ? `Run finished. No ${csvName} was created.`
          : `Run finished. ${csvName} is ready to download.`;
        finish(message);
      } else {
        finish(`Run could not finish. ${data.message}`);
      }
    };
    worker.onerror = () => finish('Python could not start. Try reloading the page or use the hosted fallback.');
    worker.postMessage({ cells: codeCells.map(({ index }) => [index, notebook.cells[index].source]),
      inputs, csv: csvName });
  });
  byId('stop').addEventListener('click', () => {
    clearResults();
    finish('Run stopped.');
  });
  byId('save').addEventListener('click', () => {
    try {
      localStorage.setItem(key, JSON.stringify({
        notebook, csv, notes: noteFields.map(field => field.value), results: Object.fromEntries(resultCount),
      }));
      announce('Saved in this browser. Download copies before switching devices.');
    } catch {
      announce('Could not save in this browser. Download copies to keep your work.');
    }
  });
  busy(false);
  announce(openMessage);
}

open().catch(() => announce('The notebook could not open. Try reloading or use the hosted fallback.'));
