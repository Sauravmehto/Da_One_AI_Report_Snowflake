/**
 * One column width for the whole chat surface.
 *
 * The empty state, the message list and the composer all have to line up: they
 * are the same column at different moments, so a width that differs between
 * them reads as the page shifting when you send your first message.
 *
 * Fluid by default (`w-full`) with a reading-width ceiling, and padding that
 * opens up as the window does — the same behaviour as Claude's transcript.
 */
export const CHAT_COLUMN = "mx-auto w-full min-w-0 max-w-3xl px-4 sm:px-6"
