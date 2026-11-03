(() => {
	// The extension APIs. Firefox names them `browser` and Chrome names them `chrome`.
	const extension = globalThis.browser ?? globalThis.chrome;
	// The message the background script sends when the toolbar button or shortcut is used.
	const toggleMessage = "toggle-feedback";
	// Where this page's notes are stored. Query strings and fragments are left out, so one page keeps one set of notes.
	const storageKey = `browser-feedback:${location.origin}${location.pathname}`;
	// The most characters of element text or attribute values kept for each element, so the packet stays readable.
	const textLimit = 240;
	// The most characters of an element's HTML kept for each element. Large containers would otherwise fill the packet.
	const htmlLimit = 1600;
	// The element that holds the extension's interface. It is added to the page only when the user first opens the panel.
	let host = null;
	// The closed shadow root that keeps page styles and scripts away from the interface.
	let shadow = null;
	// The outline that follows the pointer while picking. It ignores the pointer, so clicks reach the element underneath.
	let highlight = null;
	// The panel listing the selection and saved notes. It is rebuilt whenever either changes.
	let panel = null;
	// The live region that announces results and errors in the panel.
	let status = null;
	// The latest status text, kept so a load error from before the panel existed is still shown when it opens.
	let statusMessage = "";
	// The instruction being written, kept when the panel is rebuilt.
	let draft = "";
	// The saved notes for this page.
	let notes = [];
	// The chosen elements for the note being written.
	let selection = [];
	// Whether clicking the page chooses an element instead of doing its usual action.
	let picking = false;
	// Whether the panel is showing. It stays open after picking is paused.
	let panelOpen = false;
	// Whether a save, delete or copy is running. Panel buttons are disabled until it finishes, so one action cannot overwrite another.
	let busy = false;
	// The ID of the note being edited, or null while writing a new note.
	let editingId = null;
	// Resolves once saved notes have loaded, so the panel never opens with an empty list by mistake.
	const ready = loadNotes();

	extension.runtime.onMessage.addListener(onToggleMessage);

	/**
	 * Open or close feedback when the toolbar or shortcut sends a message.
	 *
	 * @param  {object}  message
	 *     The message from the background script.
	 */
	function onToggleMessage(message) {
		if (message?.type === toggleMessage) {
			void ready.then(togglePanel);
		}
	}

	/**
	 * Show or hide the panel. Opening it starts picking; closing it drops the unsaved selection and typed instruction.
	 */
	function togglePanel() {
		if (!host) {
			setupInterface();
			setupEvents();
		}

		panelOpen = !panelOpen;
		picking = panelOpen;

		if (!panelOpen) {
			selection = [];
			editingId = null;
			draft = "";
			hideHighlight();
		}

		render();
	}

	/**
	 * Add the panel and pointer outline to the page, inside a shadow root.
	 */
	function setupInterface() {
		host = document.createElement("browser-feedback-root");
		shadow = host.attachShadow({ mode: "closed" });
		highlight = document.createElement("div");
		panel = document.createElement("aside");
		status = document.createElement("output");
		status.textContent = statusMessage;

		const style = document.createElement("style");
		style.textContent = `
			:host { all: initial; position: fixed; inset: 0; z-index: 2147483647; pointer-events: none; font: 14px/1.5 system-ui, sans-serif; color: #17212e; }
			*, *::before, *::after { box-sizing: border-box; }
			#highlight { position: fixed; display: none; border: 3px solid #145bb5; outline: 2px solid white; border-radius: 3px; pointer-events: none; }
			#panel { position: fixed; inset: auto 16px 16px auto; display: none; width: min(380px, calc(100vw - 32px)); max-height: min(80vh, 700px); overflow: auto; padding: 16px; border: 2px solid #17212e; border-radius: 10px; background: #fff; color: #17212e; box-shadow: 0 10px 30px #0005; pointer-events: auto; }
			#panel.open { display: block; }
			h2, h3, p { margin: 0 0 10px; }
			h2 { font-size: 18px; }
			h3 { font-size: 15px; }
			label { display: block; font-weight: 600; }
			textarea { display: block; width: 100%; min-height: 78px; margin: 6px 0 10px; padding: 8px; border: 1px solid #536278; border-radius: 4px; background: #fff; color: #17212e; font: inherit; resize: vertical; }
			button { min-height: 36px; padding: 6px 10px; border: 1px solid #536278; border-radius: 4px; background: #fff; color: #17212e; font: inherit; cursor: pointer; }
			button:hover { background: #eaf2fc; }
			button:focus-visible, textarea:focus-visible { outline: 3px solid #145bb5; outline-offset: 2px; }
			button:disabled { opacity: .55; cursor: default; }
			.actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
			.note { padding: 10px 0; border-top: 1px solid #b7c1cc; }
			.note p, .selection { overflow-wrap: anywhere; }
			.muted { color: #3b4d61; }
			#status { display: block; min-height: 1.5em; margin-top: 10px; }
			@media (max-width: 400px) { #panel { inset: auto 8px 8px 8px; width: auto; max-height: 70vh; } }
		`;

		highlight.id = "highlight";
		panel.id = "panel";
		panel.setAttribute("aria-label", "Browser feedback");
		status.id = "status";
		status.setAttribute("role", "status");
		shadow.append(style, highlight, panel);
		document.documentElement.append(host);
		render();
	}

	/**
	 * Listen for the page events that picking and the panel need. Scroll and resize hide the outline whether or not picking is on.
	 */
	function setupEvents() {
		window.addEventListener("pointerover", onPointerOver, true);
		window.addEventListener("pointerdown", onPagePointerAction, true);
		window.addEventListener("mousedown", onPagePointerAction, true);
		window.addEventListener("mouseup", onPagePointerAction, true);
		window.addEventListener("click", onPageClick, true);
		window.addEventListener("keydown", onPageKeyDown, true);
		window.addEventListener("scroll", hideHighlight, true);
		window.addEventListener("resize", hideHighlight);
	}

	/**
	 * Load saved notes without trusting the stored value's shape.
	 *
	 * @returns {Promise<void>}
	 *     Resolves after notes are loaded or a storage error is reported.
	 */
	async function loadNotes() {
		try {
			const stored = await extension.storage.local.get(storageKey);
			const saved = stored[storageKey];
			notes = Array.isArray(saved) ? saved.filter(isSavedNote) : [];

			if (panel) {
				render();
			}
		} catch {
			showStatus("Could not load notes from extension storage.");
		}
	}

	/**
	 * Accept only records that the panel and packet can render safely.
	 *
	 * @param  {unknown}  note
	 *     The value read from extension storage.
	 * @returns {boolean}
	 *     Whether the value has the saved note's required fields.
	 */
	function isSavedNote(note) {
		return (
			note &&
			typeof note.id === "string" &&
			typeof note.comment === "string" &&
			Array.isArray(note.elements) &&
			note.elements.every((element) => element && typeof element.selector === "string")
		);
	}

	/**
	 * Follow the page pointer with a non-interactive outline.
	 *
	 * @param  {PointerEvent}  event
	 *     The pointer event on the page.
	 */
	function onPointerOver(event) {
		if (!picking || isInterfaceEvent(event) || !(event.target instanceof Element)) {
			return;
		}

		const bounds = event.target.getBoundingClientRect();
		highlight.style.display = "block";
		highlight.style.left = `${bounds.left}px`;
		highlight.style.top = `${bounds.top}px`;
		highlight.style.width = `${bounds.width}px`;
		highlight.style.height = `${bounds.height}px`;
	}

	/**
	 * Keep the page from reacting to presses while picking, so its links, buttons and drag handlers stay inactive.
	 *
	 * @param  {PointerEvent|MouseEvent}  event
	 *     The pointer or mouse event on the page.
	 */
	function onPagePointerAction(event) {
		if (!picking || isInterfaceEvent(event)) {
			return;
		}

		event.preventDefault();
		event.stopImmediatePropagation();
	}

	/**
	 * Select a page element without activating its usual click action.
	 *
	 * @param  {MouseEvent}  event
	 *     The click event on the page.
	 */
	function onPageClick(event) {
		if (!picking || isInterfaceEvent(event)) {
			return;
		}

		event.preventDefault();
		event.stopImmediatePropagation();

		if (event.target instanceof Element) {
			chooseElement(event.target, event.shiftKey);
		}
	}

	/**
	 * Let Escape back out and Enter select the focused page element.
	 *
	 * @param  {KeyboardEvent}  event
	 *     The key event from the page or feedback panel.
	 */
	function onPageKeyDown(event) {
		if (!panelOpen) {
			return;
		}

		if (
			event.key === "Escape" &&
			(isInterfaceEvent(event) || picking || selection.length || editingId)
		) {
			event.preventDefault();
			event.stopImmediatePropagation();
			selection = [];
			editingId = null;
			draft = "";
			picking = false;
			hideHighlight();
			render();
			panel.querySelector("button")?.focus();
			return;
		}

		if (isInterfaceEvent(event)) {
			return;
		}

		if (
			picking &&
			event.key === "Enter" &&
			event.target instanceof Element &&
			event.target !== document.body
		) {
			event.preventDefault();
			event.stopImmediatePropagation();
			chooseElement(event.target, event.shiftKey);
		}
	}

	/**
	 * Exclude the shadow interface from page-element picking.
	 *
	 * @param  {Event}  event
	 *     The event whose path may contain the panel.
	 * @returns {boolean}
	 *     Whether the event came from inside this extension's host.
	 */
	function isInterfaceEvent(event) {
		return event.composedPath().includes(host);
	}

	/**
	 * Choose an element for the next note, replacing the selection or, with Shift, adding or removing it.
	 * A snapshot is taken now, so later page changes do not alter the note.
	 *
	 * @param  {Element}  element
	 *     The page element selected by the user.
	 * @param  {boolean}  append
	 *     Whether Shift was held, which adds or removes the element instead of replacing the selection.
	 */
	function chooseElement(element, append) {
		const snapshot = captureElement(element);
		const existingIndex = selection.findIndex(
			(selected) => selected.selector === snapshot.selector,
		);

		if (append && existingIndex !== -1) {
			selection = selection.filter((_, index) => index !== existingIndex);
		} else {
			selection = append ? [...selection, snapshot] : [snapshot];
		}

		hideHighlight();
		render();
		panel.querySelector("textarea")?.focus();
	}

	/**
	 * Hide the pointer outline, which no longer matches the element after scrolling or once it is chosen.
	 */
	function hideHighlight() {
		highlight.style.display = "none";
	}

	/**
	 * Capture enough element context to locate and discuss a visual issue.
	 *
	 * @param  {Element}  element
	 *     The page element being added to a note.
	 * @returns {object}
	 *     A bounded snapshot of the element and its surroundings.
	 */
	function captureElement(element) {
		const bounds = element.getBoundingClientRect();
		const styles = getComputedStyle(element);
		const attributes = {};
		const parent = element.parentElement;
		const parentText = parent?.textContent?.trim().replace(/\s+/g, " ").slice(0, 80) ?? "";
		const parentSummary = parent ? `${parent.tagName.toLowerCase()} ${parentText}` : "";

		for (const name of [
			"alt",
			"aria-label",
			"href",
			"name",
			"placeholder",
			"src",
			"title",
			"type",
		]) {
			const value = element.getAttribute(name);

			if (value) {
				attributes[name] = value.slice(0, textLimit);
			}
		}

		return {
			selector: uniqueSelector(element),
			tag: element.tagName.toLowerCase(),
			text: element.textContent?.trim().replace(/\s+/g, " ").slice(0, textLimit) ?? "",
			id: element.id,
			classes: Array.from(element.classList).slice(0, 12),
			attributes,
			outerHTML: element.outerHTML.slice(0, htmlLimit),
			parent: parentSummary,
			previousSibling: element.previousElementSibling?.tagName.toLowerCase() ?? "",
			nextSibling: element.nextElementSibling?.tagName.toLowerCase() ?? "",
			role: element.getAttribute("role") ?? "",
			styles: {
				color: styles.color,
				backgroundColor: styles.backgroundColor,
				fontFamily: styles.fontFamily,
				fontSize: styles.fontSize,
				fontWeight: styles.fontWeight,
				lineHeight: styles.lineHeight,
				display: styles.display,
				position: styles.position,
			},
			bounds: {
				x: Math.round(bounds.x),
				y: Math.round(bounds.y),
				width: Math.round(bounds.width),
				height: Math.round(bounds.height),
			},
		};
	}

	/**
	 * Prefer a unique ID, then an ancestry path with sibling positions.
	 *
	 * @param  {Element}  element
	 *     The element to find again in the current document.
	 * @returns {string}
	 *     A selector that identifies the element in the current document.
	 */
	function uniqueSelector(element) {
		if (element === document.documentElement) {
			return "html";
		}

		if (element.id) {
			const byId = `#${CSS.escape(element.id)}`;

			if (document.querySelectorAll(byId).length === 1) {
				return byId;
			}
		}

		const parts = [];
		let current = element;

		while (current && current !== document.documentElement) {
			const tag = current.tagName.toLowerCase();
			const siblings = Array.from(current.parentElement?.children ?? []).filter(
				(sibling) => sibling.tagName === current.tagName,
			);
			const position = siblings.indexOf(current) + 1;

			parts.unshift(`${tag}:nth-of-type(${position})`);
			current = current.parentElement;
		}

		return `html > ${parts.join(" > ")}`;
	}

	/**
	 * Rebuild the panel from the current notes and selection. Page content is added as text, never as HTML.
	 */
	function render() {
		panel.replaceChildren();
		panel.classList.toggle("open", panelOpen);

		if (!panelOpen) {
			return;
		}

		const heading = document.createElement("h2");
		heading.textContent = "Browser feedback";
		panel.append(heading);

		const hint = document.createElement("p");
		hint.className = "muted";
		hint.textContent = picking
			? "Click an element. Shift-click to add more. Press Escape to cancel."
			: "Picking is paused.";
		panel.append(hint);

		const controls = document.createElement("div");
		controls.className = "actions";
		controls.append(
			button(picking ? "Pause picking" : "Pick elements", () => {
				picking = !picking;
				hideHighlight();
				render();
			}),
			button("Close panel", togglePanel),
		);
		panel.append(controls);

		const selected = document.createElement("p");
		selected.className = "selection";
		selected.textContent = selection.length
			? `Selected: ${selection.map((element) => element.selector).join(", ")}`
			: "No elements selected.";
		panel.append(selected);

		const label = document.createElement("label");
		label.htmlFor = "comment";
		label.textContent = editingId ? "Edit instruction" : "Instruction for the selected elements";

		const input = document.createElement("textarea");
		input.id = "comment";
		input.value = draft;
		input.addEventListener("input", () => {
			draft = input.value;
		});
		panel.append(label, input);

		const editorActions = document.createElement("div");
		editorActions.className = "actions";
		editorActions.append(
			button(editingId ? "Save changes" : "Save note", () => void saveNote(input.value)),
		);

		if (editingId) {
			editorActions.append(
				button("Cancel edit", () => {
					editingId = null;
					selection = [];
					draft = "";
					render();
				}),
			);
		}
		panel.append(editorActions);

		const savedHeading = document.createElement("h3");
		savedHeading.textContent = `Saved notes (${notes.length})`;
		panel.append(savedHeading);

		for (const [index, note] of notes.entries()) {
			const item = document.createElement("section");
			item.className = "note";

			const summary = document.createElement("p");
			summary.textContent = `${index + 1}. ${note.comment}`;

			const actions = document.createElement("div");
			actions.className = "actions";
			actions.append(
				button(`Edit note ${index + 1}`, () => {
					editingId = note.id;
					selection = note.elements;
					draft = note.comment;
					picking = false;
					render();
					panel.querySelector("textarea")?.focus();
				}),
				button(`Delete note ${index + 1}`, () => void deleteNote(note.id)),
			);

			item.append(summary, actions);
			panel.append(item);
		}

		if (notes.length) {
			const copyAction = button("Copy all", () => void copyAll());
			panel.append(copyAction);
		}

		panel.append(status);
	}

	/**
	 * Create a panel button that runs an action when clicked. It starts disabled while a save, delete or copy runs.
	 *
	 * @param  {string}  label
	 *     The visible action name.
	 * @param  {Function}  action
	 *     The action to run when the control is clicked.
	 * @returns {HTMLButtonElement}
	 *     The button for the panel.
	 */
	function button(label, action) {
		const control = document.createElement("button");
		control.type = "button";
		control.textContent = label;
		control.disabled = busy;
		control.addEventListener("click", action);
		return control;
	}

	/**
	 * Record whether a save, delete or copy is running, and disable the panel buttons while it is.
	 *
	 * @param  {boolean}  value
	 *     Whether an action is running.
	 */
	function setBusy(value) {
		busy = value;
		for (const control of panel.querySelectorAll("button")) {
			control.disabled = value;
		}
	}

	/**
	 * Show the result of an action in the panel's status line, which screen readers announce.
	 *
	 * @param  {string}  message
	 *     The result or error to show in the panel.
	 */
	function showStatus(message) {
		statusMessage = message;

		if (status) {
			status.textContent = message;
		}
	}

	/**
	 * Save a new note or replace an edited note after storage succeeds.
	 *
	 * @param  {string}  comment
	 *     The instruction written in the panel.
	 * @returns {Promise<void>}
	 *     Resolves after the note is saved or an error is shown.
	 */
	async function saveNote(comment) {
		const cleanComment = comment.trim();

		if (!cleanComment) {
			showStatus("Write an instruction before saving.");
			return;
		}

		if (!selection.length) {
			showStatus("Select at least one page element before saving.");
			return;
		}

		if (busy) {
			return;
		}

		const updated = editingId
			? notes.map((note) =>
					note.id === editingId ? { ...note, comment: cleanComment, elements: selection } : note,
				)
			: [
					...notes,
					{
						id: crypto.randomUUID(),
						comment: cleanComment,
						elements: selection,
						pageUrl: location.href,
						viewport: viewport(),
					},
				];

		setBusy(true);

		// Whether storage accepted the note. Only then is the panel rebuilt and focus moved, so a failed save leaves focus where it was.
		let saved = false;

		try {
			await extension.storage.local.set({ [storageKey]: updated });
			notes = updated;
			selection = [];
			editingId = null;
			picking = false;
			draft = "";
			saved = true;
		} catch {
			showStatus("Could not save the note. Try again.");
		} finally {
			setBusy(false);

			if (saved) {
				render();
				showStatus("Note saved.");
				panel.querySelector("button")?.focus();
			}
		}
	}

	/**
	 * Ask the user to confirm, then delete the note once storage has been updated. Cancel leaves the note unchanged.
	 *
	 * @param  {string}  id
	 *     The saved note to remove.
	 * @returns {Promise<void>}
	 *     Resolves after storage is updated or an error is shown.
	 */
	async function deleteNote(id) {
		if (busy || !window.confirm("Delete this note?")) {
			return;
		}

		const deletedIndex = notes.findIndex((note) => note.id === id);
		const updated = notes.filter((note) => note.id !== id);

		setBusy(true);

		// Whether storage accepted the deletion. Only then is the panel rebuilt and focus moved to a nearby note.
		let deleted = false;

		try {
			await extension.storage.local.set({ [storageKey]: updated });
			notes = updated;

			if (editingId === id) {
				editingId = null;
				selection = [];
				draft = "";
			}

			deleted = true;
		} catch {
			showStatus("Could not delete the note. Try again.");
		} finally {
			setBusy(false);

			if (deleted) {
				render();
				showStatus("Note deleted.");

				const nextIndex = Math.min(deletedIndex, updated.length - 1);
				const nextButton =
					nextIndex >= 0
						? panel.querySelectorAll(".note button")[nextIndex * 2]
						: panel.querySelector("button");

				nextButton?.focus();
			}
		}
	}

	/**
	 * Copy the packet before clearing saved notes.
	 *
	 * @returns {Promise<void>}
	 *     Resolves after copy and clear complete or an error is shown.
	 */
	async function copyAll() {
		if (busy || !notes.length) {
			return;
		}

		setBusy(true);

		// Whether the packet was copied and the notes cleared. Only then is the emptied panel rebuilt.
		let copied = false;

		try {
			await writeClipboard(renderPacket(notes));
			await extension.storage.local.remove(storageKey);
			notes = [];
			selection = [];
			editingId = null;
			draft = "";
			copied = true;
		} catch {
			showStatus(
				"Could not copy or clear the notes. They are still listed here; check your clipboard before trying again.",
			);
		} finally {
			setBusy(false);

			if (copied) {
				render();
				showStatus("Copied all notes and cleared them from this page.");
				panel.querySelector("button")?.focus();
			}
		}
	}

	/**
	 * Copy text to the clipboard. When the page blocks the clipboard API, such as on plain HTTP pages,
	 * fall back to selecting a hidden textarea and copying that.
	 *
	 * @param  {string}  text
	 *     The Markdown packet to copy.
	 * @returns {Promise<void>}
	 *     Resolves when the packet is copied; rejects if both methods fail.
	 */
	async function writeClipboard(text) {
		try {
			await navigator.clipboard.writeText(text);
			return;
		} catch {
			const fallback = document.createElement("textarea");
			fallback.value = text;
			fallback.style.position = "fixed";
			fallback.style.left = "-9999px";

			shadow.append(fallback);
			fallback.select();

			const copied = document.execCommand("copy");
			fallback.remove();

			if (!copied) {
				throw new Error("Clipboard copy failed");
			}
		}
	}

	/**
	 * Describe the visible area of the browser window for the packet.
	 *
	 * @returns {string}
	 *     The width and height in CSS pixels, such as `1280x720`.
	 */
	function viewport() {
		return `${window.innerWidth}x${window.innerHeight}`;
	}

	/**
	 * Format the notes as a numbered Markdown packet in the same layout as review-feedback's.
	 *
	 * @param  {object[]}  savedNotes
	 *     Notes for the current page.
	 * @returns {string}
	 *     A Markdown packet containing every saved note.
	 */
	function renderPacket(savedNotes) {
		const intro =
			"Please work through the feedback below. Ask any clarifying questions before making changes. When the fixes are done, show a before and after for each item, so I don't have to find it in the source again.";
		const entries = savedNotes.map((note, index) => {
			const selectors = note.elements.map((element) => element.selector).join(", ");
			const locationText =
				`${note.pageUrl ?? location.href} | viewport ${note.viewport ?? viewport()} | ${selectors}`.replaceAll(
					"`",
					"%60",
				);
			const excerpt = note.elements.map((element) => JSON.stringify(element, null, 2)).join("\n\n");
			const fence = "`".repeat(
				Math.max(3, ...Array.from(excerpt.matchAll(/`+/g), (match) => match[0].length + 1)),
			);
			const heading = `### ${index + 1}. \`${locationText}\``;
			const codeBlock = [`${fence}json`, excerpt, fence].join("\n");

			return [heading, codeBlock, note.comment].join("\n\n");
		});

		return [intro, ...entries].join("\n\n") + "\n";
	}
})();
