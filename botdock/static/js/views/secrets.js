// Secrets tab: env vars. Values stay masked; revealing fetches one value and it is dropped again on
// hide, after a timeout, and always when leaving the tab.
import { h, icon, setChildren } from "../dom.js";
import { api, post, enc } from "../api.js";
import { busy, confirmDialog, openDialog, toast, toastError, copyText, fmtRelative, spinnerBlock, iconBtn, emptyState, plural } from "../ui.js";

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const REVEAL_MS = 60000;
// Attributes that keep browsers and password managers from saving/autofilling secret fields.
const NO_SAVE = { autocomplete: "off", spellcheck: false, autocapitalize: "off", autocorrect: "off",
  "data-lpignore": "true", "data-1p-ignore": "true", "data-bwignore": "true", "data-form-type": "other" };

function keyProblem(k) {
  if (!k) return "Enter a name.";
  if (k.length > 128) return "Names can be at most 128 characters.";
  if (!KEY_RE.test(k)) return "Use letters, digits and _ only, not starting with a digit.";
  return null;
}

/** Masked multi-line value input with a show/hide toggle (a textarea, so no password-manager prompts). */
function secretInput({ id, placeholder = "Value", value = "" } = {}) {
  const ta = h("textarea", { class: "input mono secret-input masked", id, rows: "2", placeholder, ...NO_SAVE, wrap: "soft" });
  ta.value = value;
  const toggle = h("button", { class: "icon-btn secret-toggle", type: "button", "aria-label": "Show value", title: "Show value" }, icon("eye"));
  toggle.addEventListener("click", () => {
    const masked = ta.classList.toggle("masked");
    setChildren(toggle, icon(masked ? "eye" : "eyeOff"));
    toggle.setAttribute("aria-label", masked ? "Show value" : "Hide value");
    toggle.title = toggle.getAttribute("aria-label");
  });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.altKey) { e.preventDefault(); ta.form?.requestSubmit(); }
  });
  return { el: h("div", { class: "secret-field" }, ta, toggle), input: ta, wipe() { ta.value = ""; } };
}

export function mount(panel, ctx) {
  const id = ctx.botId;
  let alive = true;
  let vars = [];
  const revealed = new Map(); // key -> {cell, timer}
  const wipers = new Set();   // functions that clear secret inputs (dialogs, add form)

  const list = h("div", { class: "secrets-list" }, spinnerBlock("Loading secrets…"));
  const addBtn = h("button", { class: "btn btn-primary", type: "button", onclick: () => addForm.open() }, icon("plus"), h("span", { class: "btn-label" }, "Add secret"));
  const pasteBtn = h("button", { class: "btn", type: "button", onclick: pasteEnv }, icon("clipboard"), h("span", { class: "btn-label" }, "Paste .env"));

  panel.append(h("div", { class: "secrets" },
    h("div", { class: "section-head" },
      h("div", null,
        h("h2", { class: "section-title" }, "Environment variables"),
        h("p", { class: "muted section-sub" }, "Passed to the bot as environment variables (read them with os.environ). Encrypted at rest; changes apply on the next restart.")),
      h("div", { class: "section-actions" }, pasteBtn, addBtn)),
    list));

  // ---- add form (inline row under the table)
  const addForm = (() => {
    const err = h("div", { class: "field-error", role: "alert" });
    const key = h("input", { class: "input mono", type: "text", placeholder: "DISCORD_TOKEN", "aria-label": "Name", maxlength: "128", ...NO_SAVE });
    const val = secretInput({ placeholder: "Value" });
    wipers.add(val.wipe);
    const save = h("button", { class: "btn btn-primary", type: "submit" }, h("span", { class: "btn-label" }, "Save"));
    const el = h("form", { class: "secret-add card", hidden: true, novalidate: true, autocomplete: "off", onsubmit: (e) => {
      e.preventDefault();
      const k = key.value.trim();
      const p = keyProblem(k);
      if (p) { err.textContent = p; key.focus(); return; }
      busy(save, async () => {
        if (vars.some((v) => v.key === k)) {
          const ok = await confirmDialog({ title: `Replace ${k}?`, message: `${k} already exists. Saving replaces its current value.`, confirmText: "Replace" });
          if (!ok) return;
        }
        try {
          await api(`/api/bots/${enc(id)}/env/${enc(k)}`, { method: "PUT", json: { value: val.input.value } });
          toast(`Saved ${k}`, "success");
          key.value = ""; val.wipe(); err.textContent = "";
          el.hidden = true;
          await reload();
        } catch (e2) { err.textContent = e2.message; }
      });
    } },
    h("div", { class: "secret-add-grid" },
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Name"), key),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Value"), val.el),
      h("div", { class: "secret-add-actions" },
        h("button", { class: "btn", type: "button", onclick: () => { el.hidden = true; val.wipe(); key.value = ""; err.textContent = ""; } }, "Cancel"),
        save)),
    err);
    return { el, open() { el.hidden = false; key.focus(); el.scrollIntoView({ block: "nearest" }); } };
  })();
  panel.querySelector(".secrets").append(addForm.el);

  // ---- table
  function render() {
    hideAll();
    if (!vars.length) {
      setChildren(list, emptyState({ iconName: "key", title: "No secrets yet",
        text: "Add your bot token and other settings here instead of hard-coding them. Upload a .env file or paste one to import many at once." }));
      return;
    }
    const rows = vars.map((v) => {
      const cell = h("div", { class: "secret-value mono" }, h("span", { class: "masked-text" }, v.masked));
      const eye = iconBtn("eye", `Reveal ${v.key}`, () => toggleReveal(v, cell, eye));
      return h("div", { class: "secret-row", role: "row" },
        h("div", { class: "secret-key mono", role: "cell" }, v.key),
        h("div", { class: "secret-cell", role: "cell" }, cell),
        h("div", { class: "secret-meta faint", role: "cell", title: v.updated_at ? new Date(v.updated_at).toLocaleString() : "" },
          `${v.length} chars · ${fmtRelative(v.updated_at)}`),
        h("div", { class: "secret-actions", role: "cell" },
          eye,
          iconBtn("copy", `Copy ${v.key}`, (e) => copyValue(v, e.currentTarget)),
          iconBtn("edit", `Edit ${v.key}`, () => editValue(v)),
          iconBtn("trash", `Delete ${v.key}`, () => remove(v), "danger-hover")));
    });
    setChildren(list, h("div", { class: "secret-table card", role: "table", "aria-label": "Environment variables" },
      h("div", { class: "secret-row secret-head", role: "row" },
        h("div", { role: "columnheader" }, "Name"), h("div", { role: "columnheader" }, "Value"),
        h("div", { role: "columnheader" }, "Updated"), h("div", { role: "columnheader", class: "sr-only-text" }, "Actions")),
      rows),
    h("p", { class: "faint small" }, `${plural(vars.length, "variable")}`));
  }

  async function fetchValue(key) {
    const d = await api(`/api/bots/${enc(id)}/env/${enc(key)}`);
    return d.value;
  }

  function hide(key) {
    const r = revealed.get(key);
    if (!r) return;
    clearTimeout(r.timer);
    const v = vars.find((x) => x.key === key);
    setChildren(r.cell, h("span", { class: "masked-text" }, v ? v.masked : "••••••••"));
    r.cell.classList.remove("revealed");
    setChildren(r.eye, icon("eye"));
    r.eye.setAttribute("aria-label", `Reveal ${key}`);
    r.eye.title = r.eye.getAttribute("aria-label");
    revealed.delete(key);
  }

  function hideAll() { for (const k of [...revealed.keys()]) hide(k); }

  async function toggleReveal(v, cell, eye) {
    if (revealed.has(v.key)) { hide(v.key); return; }
    await busy(eye, async () => {
      try {
        const value = await fetchValue(v.key);
        if (!alive) return;
        setChildren(cell, h("span", { class: "plain-text" }, value === "" ? "(empty)" : value));
        cell.classList.add("revealed");
        setChildren(eye, icon("eyeOff"));
        eye.setAttribute("aria-label", `Hide ${v.key}`);
        eye.title = eye.getAttribute("aria-label");
        revealed.set(v.key, { cell, eye, timer: setTimeout(() => hide(v.key), REVEAL_MS) });
      } catch (e) { toastError(e, `Couldn't reveal ${v.key}`); }
    });
  }

  async function copyValue(v, button) {
    await busy(button, async () => {
      try {
        let value = await fetchValue(v.key);
        const ok = await copyText(value);
        value = null;
        if (ok) toast(`Copied ${v.key} to the clipboard`, "success");
        else toast("Your browser blocked clipboard access. Reveal the value and copy it manually.", "error");
      } catch (e) { toastError(e, `Couldn't copy ${v.key}`); }
    });
  }

  async function editValue(v) {
    let value;
    try { value = await fetchValue(v.key); } catch (e) { toastError(e, `Couldn't load ${v.key}`); return; }
    if (!alive) return;
    let field = null;
    await openDialog({
      title: `Edit ${v.key}`,
      onClose: () => { if (field) field.wipe(); },
      build(close) {
        field = secretInput({ id: "secret-edit", value });
        value = null;
        const err = h("div", { class: "field-error", role: "alert" });
        const save = h("button", { class: "btn btn-primary", type: "submit" }, h("span", { class: "btn-label" }, "Save"));
        setTimeout(() => field.input.focus());
        return h("form", { class: "dialog-form", autocomplete: "off", novalidate: true, onsubmit: (e) => {
          e.preventDefault();
          busy(save, async () => {
            try {
              await api(`/api/bots/${enc(id)}/env/${enc(v.key)}`, { method: "PUT", json: { value: field.input.value } });
              toast(`Saved ${v.key}`, "success");
              close(true);
              reload();
            } catch (e2) { err.textContent = e2.message; }
          });
        } },
        h("div", { class: "dialog-body" },
          h("label", { class: "field", for: "secret-edit" }, h("span", { class: "field-label" }, "Value"), field.el,
            h("span", { class: "field-hint" }, "Shift+Enter for a new line. A running bot sees the new value after a restart.")),
          err),
        h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(false) }, "Cancel"), save));
      },
    });
    field = null;
  }

  async function remove(v) {
    const ok = await confirmDialog({ title: `Delete ${v.key}?`, message: "The variable is removed from BotDock. A running bot keeps the old value until it restarts.", confirmText: "Delete", danger: true });
    if (!ok) return;
    try {
      await api(`/api/bots/${enc(id)}/env/${enc(v.key)}`, { method: "DELETE" });
      toast(`Deleted ${v.key}`, "success");
      reload();
    } catch (e) { toastError(e, `Couldn't delete ${v.key}`); }
  }

  async function pasteEnv() {
    let ta = null;
    await openDialog({
      title: "Import from .env",
      wide: true,
      onClose: () => { if (ta) ta.value = ""; },
      build(close) {
        ta = h("textarea", { class: "input mono env-paste", rows: "10", placeholder: "DISCORD_TOKEN=...\nGUILD_ID=1234\n# comments and export KEY=value are fine", ...NO_SAVE, autofocus: true });
        const overwrite = h("input", { type: "checkbox", checked: true });
        const err = h("div", { class: "field-error", role: "alert" });
        const go = h("button", { class: "btn btn-primary", type: "submit" }, h("span", { class: "btn-label" }, "Import"));
        return h("form", { class: "dialog-form", autocomplete: "off", onsubmit: (e) => {
          e.preventDefault();
          if (!ta.value.trim()) { err.textContent = "Paste the contents of a .env file first."; return; }
          busy(go, async () => {
            try {
              const r = await post(`/api/bots/${enc(id)}/env/import`, { text: ta.value, overwrite: overwrite.checked });
              ta.value = "";
              const imp = r.imported || [], sk = r.skipped || [];
              toast(imp.length ? `Imported ${plural(imp.length, "variable")}` : "Nothing was imported", imp.length ? "success" : "info",
                { detail: [imp.join(", "), sk.length ? `Skipped (already set): ${sk.join(", ")}` : ""].filter(Boolean).join(" · ") });
              close(true);
              reload();
            } catch (e2) { err.textContent = e2.message; }
          });
        } },
        h("div", { class: "dialog-body" },
          h("p", { class: "muted" }, "One KEY=value per line. Quotes are removed; lines starting with # are ignored."),
          ta,
          h("label", { class: "check" }, overwrite, h("span", null, "Overwrite variables that already exist")),
          err),
        h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(false) }, "Cancel"), go));
      },
    });
    ta = null;
  }

  async function reload() {
    try {
      const d = await api(`/api/bots/${enc(id)}/env`);
      if (!alive) return;
      vars = d.vars || [];
      render();
    } catch (e) {
      if (!alive) return;
      toastError(e, "Couldn't load secrets");
      setChildren(list, emptyState({ iconName: "alert", title: "Couldn't load secrets", text: e.message,
        action: h("button", { class: "btn", type: "button", onclick: reload }, "Try again") }));
    }
  }
  reload();

  // Hide revealed values when the page is backgrounded (phone lock, tab switch).
  const onHidden = () => { if (document.visibilityState === "hidden") hideAll(); };
  document.addEventListener("visibilitychange", onHidden);

  return {
    unmount() {
      alive = false;
      hideAll();
      for (const w of wipers) w();
      document.removeEventListener("visibilitychange", onHidden);
      // close any open secret dialog so its value leaves the DOM too
      for (const d of document.querySelectorAll("dialog")) { d.querySelectorAll("textarea, input").forEach((i) => { i.value = ""; }); d.remove(); }
      vars = [];
      setChildren(panel);
    },
  };
}
