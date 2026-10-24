/**
 * bot-bottle remote view for pi.
 *
 * A session launched from the console is driven from the phone's own message
 * box, so pi's input editor is noise there. With --bb-remote-view, the editor
 * draws nothing while it's empty and appears as usual once it has text: the
 * console's messages (typed and submitted in one write) never show, and
 * someone attached in a terminal still sees what they type.
 */

import { CustomEditor, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

class QuietEditor extends CustomEditor {
	render(width: number): string[] {
		return this.getText() ? super.render(width) : [];
	}
}

export default function (pi: ExtensionAPI) {
	pi.registerFlag("bb-remote-view", {
		description: "Hide the empty input editor (a bot-bottle console session)",
		type: "boolean",
		default: false,
	});
	pi.on("session_start", (_event, ctx) => {
		if (!pi.getFlag("bb-remote-view")) return;
		ctx.ui.setEditorComponent((tui, theme, keybindings) => new QuietEditor(tui, theme, keybindings));
	});
}
