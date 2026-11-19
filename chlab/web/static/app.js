"use strict";

const chlab = (() => {
  const POLL_INTERVAL = 800;

  async function api(path, options = {}) {
    const response = await fetch(path, {
      // X-Chlab-Request: the API refuses state changes without it, which is what stops
      // another site from posting a form at it.
      headers: { "Content-Type": "application/json", "X-Chlab-Request": "1" },
      ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.detail || `${response.status} ${response.statusText}`);
    }
    return body;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function waitForJob(jobId, onEvents) {
    for (;;) {
      const job = await api(`/api/jobs/${jobId}`);
      if (onEvents) onEvents(job.events);
      if (job.status !== "running") return job;
      await sleep(POLL_INTERVAL);
    }
  }

  // --- instance actions ----------------------------------------------------------------

  function wireInstanceActions() {
    document.querySelectorAll("[data-action][data-target]").forEach((button) => {
      button.addEventListener("click", async () => {
        const { action, target } = button.dataset;
        if (action === "destroy" && !confirm(`Delete ${target} and all of its data?`)) return;

        const original = button.textContent;
        button.disabled = true;
        button.textContent = `${action}…`;
        try {
          const { job } = await api(`/api/instances/${target}/actions/${action}`, { method: "POST" });
          const finished = await waitForJob(job.id);
          if (finished.status === "failed") throw new Error(finished.error);
          window.location.href = action === "destroy" ? "/" : window.location.pathname;
        } catch (failure) {
          alert(failure.message);
          button.disabled = false;
          button.textContent = original;
        }
      });
    });
  }

  // --- create form ---------------------------------------------------------------------

  const rowTemplates = {
    user: () => `
      <input placeholder="username" data-field="name" required>
      <input placeholder="password (optional)" data-field="password" type="text">
      <select data-field="role" title="privileges">
        <option value="readwrite">readwrite</option>
        <option value="readonly">readonly</option>
        <option value="admin">admin</option>
        <option value="none">no privileges</option>
      </select>
      <button type="button" class="remove" title="remove">&times;</button>`,
    database: () => `
      <input placeholder="database name" data-field="name" required>
      <select data-field="engine">
        <option value="Atomic">Atomic</option>
        <option value="Replicated">Replicated</option>
      </select>
      <button type="button" class="remove" title="remove">&times;</button>`,
  };

  function addRow(kind, values = {}) {
    const container = document.getElementById(kind === "user" ? "users" : "databases");
    const row = document.createElement("div");
    row.className = "repeatable-row";
    row.innerHTML = rowTemplates[kind]();
    row.querySelectorAll("[data-field]").forEach((input) => {
      const value = values[input.dataset.field];
      if (value === undefined) return;
      if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = value;
    });
    row.querySelector(".remove").addEventListener("click", () => row.remove());
    container.appendChild(row);
  }

  function collectRows(kind) {
    const container = document.getElementById(kind === "user" ? "users" : "databases");
    return [...container.querySelectorAll(".repeatable-row")]
      .map((row) => {
        const entry = {};
        row.querySelectorAll("[data-field]").forEach((input) => {
          entry[input.dataset.field] = input.type === "checkbox" ? input.checked : input.value;
        });
        return entry;
      })
      .filter((entry) => entry.name && entry.name.trim());
  }

  function parseSettings(text) {
    const settings = {};
    text.split("\n").forEach((line) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      const index = trimmed.indexOf("=");
      if (index === -1) return;
      settings[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim();
    });
    return settings;
  }

  function describeTopology(form) {
    const shards = Number(form.shards.value) || 1;
    const replicas = Number(form.replicas.value) || 1;
    const nodes = shards * replicas;
    const keeper = form.keeper_nodes.value;
    const keeperNote =
      keeper === "" ? (replicas > 1 ? "1 keeper (automatic)" : "no keeper needed")
      : keeper === "0" ? "no keeper"
      : `${keeper} keeper node(s)`;
    return `${nodes} ClickHouse node${nodes === 1 ? "" : "s"}, ${keeperNote}.`;
  }

  function wireCreateForm() {
    const form = document.getElementById("create");
    const hint = document.getElementById("topology-hint");

    document.querySelectorAll("[data-add]").forEach((button) => {
      button.addEventListener("click", () => addRow(button.dataset.add));
    });

    const refresh = () => { hint.textContent = describeTopology(form); };
    ["shards", "replicas", "keeper_nodes"].forEach((field) => {
      form[field].addEventListener("input", refresh);
      form[field].addEventListener("change", refresh);
    });
    refresh();

    if (form.profile) {
      form.profile.addEventListener("change", (event) => {
        const option = event.target.selectedOptions[0];
        const profile = option.dataset.profile ? JSON.parse(option.dataset.profile) : null;
        if (!profile) return;
        if (profile.source) form.source.value = profile.source;
        form.shards.value = profile.shards ?? 1;
        form.replicas.value = profile.replicas ?? 1;
        form.keeper_nodes.value = profile.keeper_nodes === null || profile.keeper_nodes === undefined
          ? "" : String(profile.keeper_nodes);
        form.cluster_name.value = profile.cluster_name ?? "default_cluster";
        form.seed.checked = Boolean(profile.seed);
        document.getElementById("users").innerHTML = "";
        document.getElementById("databases").innerHTML = "";
        (profile.users || []).forEach((user) =>
          addRow("user", { name: user.name, password: user.password, role: user.role }));
        (profile.databases || []).forEach((database) => addRow("database", database));
        refresh();
      });
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = form.querySelector("button[type=submit]");
      submit.disabled = true;

      const payload = {
        source: form.source.value.trim(),
        name: form.name.value.trim() || null,
        version: form.version.value.trim() || null,
        shards: Number(form.shards.value) || 1,
        replicas: Number(form.replicas.value) || 1,
        keeper_nodes: form.keeper_nodes.value === "" ? null : Number(form.keeper_nodes.value),
        cluster_name: form.cluster_name.value.trim() || "default_cluster",
        users: collectRows("user"),
        databases: collectRows("database"),
        settings: parseSettings(form.settings.value),
        seed: form.seed.checked,
        save_as_profile: form.save_as_profile.value.trim() || null,
      };

      const panel = document.getElementById("progress");
      const list = document.getElementById("progress-events");
      const errorBox = document.getElementById("progress-error");
      panel.hidden = false;
      errorBox.hidden = true;
      panel.scrollIntoView({ behavior: "smooth" });

      try {
        const { job, name } = await api("/api/instances", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        document.getElementById("progress-name").textContent = name;

        const finished = await waitForJob(job.id, (events) => {
          list.innerHTML = events.map((event) => `<li>${escapeHtml(event)}</li>`).join("");
        });

        if (finished.status === "failed") throw new Error(finished.error);
        window.location.href = `/instance/${name}`;
      } catch (failure) {
        errorBox.hidden = false;
        errorBox.textContent = failure.message;
        submit.disabled = false;
      }
    });
  }

  // --- SQL editor ----------------------------------------------------------------------

  let aceEditor = null;

  function upgradeEditor() {
    const textarea = document.getElementById("sql");
    if (!textarea || !window.ace) return;

    const host = document.createElement("div");
    host.id = "ace-editor";
    host.textContent = textarea.value;
    textarea.replaceWith(host);

    ace.config.set("basePath", "/static/vendor/ace");
    aceEditor = ace.edit(host, {
      mode: "ace/mode/sql",
      theme: matchMedia("(prefers-color-scheme: dark)").matches
        ? "ace/theme/tomorrow_night"
        : "ace/theme/tomorrow",
      fontSize: 13,
      showPrintMargin: false,
      highlightActiveLine: false,
    });
    aceEditor.commands.addCommand({
      name: "run",
      bindKey: { win: "Ctrl-Enter", mac: "Cmd-Enter" },
      exec: () => document.getElementById("run").click(),
    });
  }

  const getSql = () => (aceEditor ? aceEditor.getValue() : document.getElementById("sql").value);
  const setSql = (value) => {
    if (aceEditor) aceEditor.setValue(value, 1);
    else document.getElementById("sql").value = value;
  };

  function historyKey(instance) { return `chlab.history.${instance}`; }

  function loadHistory(instance) {
    try { return JSON.parse(localStorage.getItem(historyKey(instance))) || []; }
    catch { return []; }
  }

  function renderHistory(instance) {
    const list = document.getElementById("history");
    const entries = loadHistory(instance);
    list.innerHTML = entries
      .map((sql, index) => `<li data-index="${index}">${escapeHtml(sql.replace(/\s+/g, " ").slice(0, 90))}</li>`)
      .join("");
    list.querySelectorAll("li").forEach((item) => {
      item.addEventListener("click", () => setSql(entries[Number(item.dataset.index)]));
    });
  }

  function recordHistory(instance, sql) {
    const entries = loadHistory(instance).filter((entry) => entry !== sql);
    entries.unshift(sql);
    localStorage.setItem(historyKey(instance), JSON.stringify(entries.slice(0, 30)));
    renderHistory(instance);
  }

  let rowLimit = Infinity;

  function renderResults(results) {
    const container = document.getElementById("results");
    const agreement = document.getElementById("agreement");
    container.innerHTML = "";

    results.forEach((result) => {
      const block = document.createElement("div");
      block.className = "result-block";
      const heading = results.length > 1 ? `<h3>${escapeHtml(result.node || "")}</h3>` : "";

      if (result.error) {
        block.innerHTML = `${heading}<div class="result-error">${escapeHtml(result.error)}</div>`;
      } else if (!result.columns.length) {
        block.innerHTML = `${heading}<div class="result-meta">ok</div>`;
      } else {
        const head = result.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
        const body = result.rows
          .map((row) => `<tr>${row.map((cell) => `<td>${renderCell(cell)}</td>`).join("")}</tr>`)
          .join("");
        block.innerHTML = `
          ${heading}
          <div class="result-scroll"><table class="result"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
          <div class="result-meta">${result.rows.length} row(s), ${result.rows_read} read in ${result.elapsed.toFixed(3)}s</div>`;
      }
      container.appendChild(block);
    });

    if (results.length < 2) { agreement.textContent = ""; return; }
    const successes = results.filter((result) => !result.error);
    const fingerprints = new Set(successes.map((result) => JSON.stringify([result.columns, result.rows])));
    agreement.className = fingerprints.size === 1 ? "agree" : "disagree";
    agreement.textContent = fingerprints.size === 1
      ? `all ${successes.length} nodes agree`
      : `nodes disagree — ${fingerprints.size} distinct results`;
  }

  function renderCell(value) {
    if (value === null) return '<span class="null">NULL</span>';
    if (typeof value === "object") return escapeHtml(JSON.stringify(value));
    return escapeHtml(String(value));
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  }

  function wireEditor() {
    const section = document.querySelector(".editor");
    if (!section) return;
    const instance = section.dataset.instance;
    const runButton = document.getElementById("run");

    renderHistory(instance);

    const run = async () => {
      const sql = getSql().trim();
      if (!sql) return;
      const target = document.getElementById("target").value;

      runButton.disabled = true;
      try {
        const { results, row_limit } = await api(`/api/instances/${instance}/query`, {
          method: "POST",
          body: JSON.stringify({
            sql,
            node: target && target !== "__all__" ? target : null,
            all_nodes: target === "__all__",
            user: document.getElementById("user").value || "default",
            password: document.getElementById("password").value,
            database: document.getElementById("database").value || null,
          }),
        });
        rowLimit = row_limit ?? Infinity;
        renderResults(results);
        recordHistory(instance, sql);
      } catch (failure) {
        renderResults([{ error: failure.message, node: null }]);
      } finally {
        runButton.disabled = false;
      }
    };

    runButton.addEventListener("click", run);
    document.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); run(); }
    });
  }

  return { wireInstanceActions, wireCreateForm, wireEditor, upgradeEditor };
})();
