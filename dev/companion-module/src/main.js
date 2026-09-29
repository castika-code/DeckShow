// Companion module for Castika DeckShow (2026-09-20).
//
// This is the thin layer Companion itself runs: it owns nothing but the
// "DeckShow Button" buttons the user places, and talks to the Python adapter
// (python/adapters/companion/adapter.py, module_server.py) on 127.0.0.1. The
// adapter does the idle detection, audio analysis and drawing; this module
//   - tells the adapter which buttons exist and where (feedback subscribe)
//   - polls the adapter for new button images and hands them to Companion as an
//     advanced feedback (png64)
//   - forwards button presses (action) and the config form (settings)
// Nothing here needs Node on the user's machine: Companion runs it with its
// own bundled runtime.
const { InstanceBase, InstanceStatus, Regex, combineRgb } = require('@companion-module/base')
// The idle look of a placed button: the same icon the Stream Deck plugin shows
// (src/key_icon.js is generated from the plugin's key@2x.png by build.sh and committed, so
// the module builds on its own with yarn install + yarn package). Presets
// carry it as png64 in their style, so a freshly placed button is not just black.
const KEY_ICON = require('./key_icon.js')

// Poll rates for GET /frames. Only a running show needs the render rate; an
// idle show or a missing adapter would otherwise cost ~1% CPU forever
// (Activity Monitor showed the "castika" process at 1.2% after the adapter
// was uninstalled, 2026-09-20).
const POLL_MS = 100 // running show: ~10 fps, the adapter's render rate
const POLL_IDLE_MS = 500 // adapter up, show off: a start is noticed within 0.5 s
const POLL_DOWN_MS = 3000 // adapter not reachable: retry every 3 s
const KEYS_DEBOUNCE_MS = 150

const STYLE_CHOICES = [
	{ id: 'text', label: 'Text' },
	{ id: 'stripe', label: 'Stripe' },
	{ id: 'solid', label: 'Solid' },
	{ id: 'random', label: 'Random' },
]
// Stripe and Solid draw a meter; Text draws characters and Random cycles through
// the styles with their own settings, so it shows none of them.
const METER_STYLES = `$(options:style) == 'stripe' || $(options:style) == 'solid'`
const COLOR_CHOICES = [
	{ id: 'gradient', label: 'Gradient' },
	{ id: 'fixed', label: 'Fixed' },
]
const ROTATE_CHOICES = ['0', '90', '180', '270'].map((d) => ({ id: d, label: d + '°' }))
const ANALYSIS_CHOICES = [
	{ id: 'absolute', label: 'Absolute' },
	{ id: 'relative', label: 'Relative' },
]

function hex(n) {
	// Companion color pickers give 0xRRGGBB numbers; the adapter takes #rrggbb.
	if (typeof n !== 'number') return undefined
	return '#' + (n & 0xffffff).toString(16).padStart(6, '0')
}

class CastikaInstance extends InstanceBase {
	constructor(internal) {
		super(internal)
		this.keys = new Map() // feedback id -> { page, row, col, size, controlId }
		this.images = new Map() // feedback id -> png64 data url (null = button's own style)
		this.seq = 0
		this.running = false
		this.pollTimer = null
		this.destroyed = false
		this.keysTimer = null
		this.reachable = null
		this.boot = null // the adapter's run id: a new one means it forgot everything
		this.surfaces = [] // from the adapter: Companion's own surface list, for the per-deck page fields
		this.pages = [] // from the adapter: Companion's own pages, for the switch-to-page choice
		this.fonts = [] // from the adapter: bundled fonts + whatever the user dropped in its fonts folder
		this.fontsFolder = ''
		this.tcp = false
	}

	// Companion asks for the config fields when the form opens; refresh the
	// surface list right before, so a newly added deck shows up in the form.
	// The fonts the installation offers: the two bundled ones and every font the
	// user dropped in its fonts folder. Picking from that list means nobody has to
	// type a path (사용자 2026-09-29: "특정 폴더에 저장하면 된다는 안내 방식").
	async fetchFonts() {
		try {
			const data = await this.call('GET', '/fonts')
			this.fonts = data.fonts || []
			this.fontsFolder = data.folder || ''
		} catch (e) {
			// keep the last list
		}
	}

	async fetchSurfaces() {
		try {
			const data = await this.call('GET', '/surfaces')
			this.surfaces = data.surfaces || []
			this.pages = data.pages || []
			this.tcp = !!data.tcp
			this.fillInSurfaceDefaults()
			this.showStatus()
		} catch (e) {
			// keep the last list
		}
	}

	// A deck's fields only exist once the deck is known, so they are missing from
	// a config saved before that. Companion shows such a dropdown empty and marks
	// the form invalid, and then the Save button stays greyed out ("Please fix the
	// errors before saving", 사용자 2026-09-29). Write the defaults ourselves.
	fillInSurfaceDefaults() {
		if (!this.config) return
		const config = { ...this.config }
		let added = false
		for (const sf of this.surfaces) {
			const mode = CastikaInstance.fieldId('mode', sf.id)
			const page = CastikaInstance.fieldId('page', sf.id)
			if (config[mode] === undefined) { config[mode] = 'keys'; added = true }
			if (config[page] === undefined) { config[page] = 1; added = true }
		}
		if (!added) return
		this.config = config
		this.saveConfig(config)
	}

	static fieldId(prefix, surfaceId) {
		return prefix + '_' + surfaceId.replace(/[^A-Za-z0-9]/g, '_')
	}

	async init(config) {
		this.config = config
		this.setActionDefinitions(this.actions())
		this.setFeedbackDefinitions(this.feedbacks())
		this.setPresetDefinitions(...this.presets())
		this.updateStatus(InstanceStatus.Connecting)
		await this.fetchSurfaces()
		await this.fetchFonts()
		await this.pushSettings()
		this.schedulePoll(0)
		this.surfacesTimer = setInterval(() => this.fetchSurfaces(), 10000)
	}

	async destroy() {
		this.destroyed = true
		clearTimeout(this.pollTimer)
		clearInterval(this.surfacesTimer)
		clearTimeout(this.keysTimer)
	}

	async configUpdated(config) {
		this.config = config
		await this.fetchFonts()
		await this.fetchSurfaces()
		this.showStatus()
		await this.pushSettings()
	}

	getConfigFields() {
		return [
			// Each group opens with its own heading: the label sits in the left column,
			// so the value starts with a rule ('---') that runs beside it, then the text.
			// An empty row before a heading keeps the groups apart (사용자 2026-09-29).
			// The form is the only place a Companion user reads about these settings, so
			// each group opens with a line of its own. A static-text with a label draws
			// its value full width, which is how '---' becomes a rule across the panel.
			{ type: 'static-text', id: 'info', width: 12, label: ' ', value:
				'Settings for Castika DeckShow. They apply to every DeckShow Button you place.\n\n---' },
			{ type: 'static-text', id: 'adapter_info', width: 12, label: ' ', value:
				'The adapter is the small program in the Castika DeckShow package that listens to the room. ' +
				'It already runs if you installed that package.' },
			{ type: 'checkbox', id: 'show', label: 'Show On/Off', width: 4, default: true },
			{ type: 'textinput', id: 'host', label: 'Adapter host', width: 8, default: '127.0.0.1', regex: Regex.IP },
			{ type: 'number', id: 'port', label: 'Adapter port', width: 4, default: 18790, min: 1, max: 65535 },
			// isVisibleExpression may only name a field that has disableAutoExpression,
			// so Style carries that (it is a plain choice, not something to drive with a
			// variable). The order follows the Stream Deck panel.
			{ type: 'static-text', id: 'gap_style', width: 12, label: '', value: '' },
			{ type: 'static-text', id: 'sep_style', width: 12, label: ' ', value:
				'---\n\n**Show style**\n\nWhat is drawn. Only the settings the chosen style uses are shown.' +
				(this.fontsFolder ? `\n\nFor the Text style: drop .ttf, .otf, .ttc or .woff files in ${this.fontsFolder} and reopen this page to pick them.` : '') },
			{ type: 'dropdown', id: 'style', label: 'Style', width: 6, default: 'text', disableAutoExpression: true, choices: STYLE_CHOICES },
			{ type: 'dropdown', id: 'font', label: 'Font', width: 12, default: '', allowCustom: true,
				choices: [{ id: '', label: 'Built-in' }].concat(this.fonts.map((f) => ({ id: f.path, label: f.name }))),
				isVisibleExpression: `$(options:style) == 'text'` },
			{ type: 'textinput', id: 'text', label: 'Text', width: 12, default: '',
				isVisibleExpression: `$(options:style) == 'text'` },
			// The peak line is lit as one rung of the ladder, so only Stripe has it.
			{ type: 'checkbox', id: 'peak', label: 'Peak hold', width: 4, default: true,
				isVisibleExpression: `$(options:style) == 'stripe'` },
			{ type: 'dropdown', id: 'color', label: 'Color', width: 6, default: 'fixed', choices: COLOR_CHOICES,
				isVisibleExpression: METER_STYLES },
			{ type: 'colorpicker', id: 'color_high', label: 'Top', width: 4, default: combineRgb(255, 40, 40),
				isVisibleExpression: METER_STYLES },
			{ type: 'colorpicker', id: 'color_mid', label: 'Middle', width: 4, default: combineRgb(255, 220, 0),
				isVisibleExpression: METER_STYLES },
			{ type: 'colorpicker', id: 'color_low', label: 'Bottom', width: 4, default: combineRgb(0, 230, 90),
				isVisibleExpression: METER_STYLES },
			{ type: 'number', id: 'bars', label: 'Bars per button', width: 8, default: 4, min: 1, max: 14, step: 1, asInteger: true,
				isVisibleExpression: METER_STYLES },
			{ type: 'static-text', id: 'gap_all', width: 12, label: '', value: '' },
			{ type: 'static-text', id: 'sep_all', width: 12, label: ' ', value:
				'---\n\n**How the show runs**\n\nStart after is in idle minutes, Gate in milliseconds (0 = off). ' +
				'Stop on use ends the show as soon as the keyboard or mouse is used. ' +
				'Analysis: Absolute follows the room as it is, Relative keeps the meter in range.' },
			{ type: 'number', id: 'start_after', label: 'Start after', width: 8, default: 1, min: 0.5, max: 30, step: 0.5 },
			{ type: 'checkbox', id: 'stop_on_activity', label: 'Stop on use', width: 4, default: true },
			{ type: 'dropdown', id: 'rotate', label: 'Rotate', width: 4, default: '0', choices: ROTATE_CHOICES },
			{ type: 'number', id: 'sensitivity', label: 'Sensitivity', width: 8, default: 1.0, min: 0.25, max: 4, step: 0.25 },
			{ type: 'number', id: 'gate_ms', label: 'Gate', width: 8, default: 0, min: 0, max: 1000, step: 50, asInteger: true },
			{ type: 'dropdown', id: 'analysis', label: 'Analysis', width: 4, default: 'relative', choices: ANALYSIS_CHOICES },
			...this.surfaceFields(),
		]
	}

	// Per deck (사용자 제안 2026-09-20): whether it takes part at all, and whether
	// it is switched to a page of its own when the show starts.
	//   off   -> the deck is left alone (not switched, its page not drawn)
	//   keys  -> only the DeckShow Buttons on whatever page it shows take part (the id stays 'keys')
	//   page  -> the deck is switched to page N (filled with DeckShow Buttons)
	//            when the show starts, and back when it stops
	surfaceFields() {
		const fields = [
			{ type: 'static-text', id: 'gap_decks', width: 12, label: '', value: '' },
			{ type: 'static-text', id: 'decks_info', width: 12, label: ' ',
				value: this.surfaces.length
					? '---\n\n**Decks**'
					: '---\n\n**Decks**\n\nNo decks reported yet (is the Castika DeckShow installation running?). Reopen this page after a moment.' },
		]
		for (const sf of this.surfaces) {
			const mode = CastikaInstance.fieldId('mode', sf.id)
			const grid = Array.isArray(sf.keys) && sf.keys.length === 2 ? ` ${sf.keys[0]}x${sf.keys[1]}` : ''
			fields.push(
				{ type: 'dropdown', id: mode, width: 8, label: (sf.short || sf.name) + grid,
					default: 'keys', disableAutoExpression: true, choices: [
					{ id: 'off', label: 'Show off on this deck' },
					{ id: 'keys', label: 'Show on its buttons' },
					{ id: 'page', label: 'Show on a specific page and back' },
				] },
				// A page that does not exist would leave the deck nowhere, so the
				// choice is Companion's own page list (사용자 2026-09-29).
				this.pages.length
					? { type: 'dropdown', id: CastikaInstance.fieldId('page', sf.id), width: 4, label: 'Page',
						default: this.pages[0].n, disableAutoExpression: true,
						choices: this.pages.map((p) => ({ id: p.n, label: p.name ? `${p.n}  ${p.name}` : String(p.n) })),
						isVisibleExpression: `$(options:${mode}) == 'page'` }
					: { type: 'number', id: CastikaInstance.fieldId('page', sf.id), width: 4, label: 'Page', default: 1, min: 1, max: 99, step: 1,
						isVisibleExpression: `$(options:${mode}) == 'page'` },
			)
			// Only switching pages needs the TCP API, so the note belongs to that choice
			// and shows up when it is picked (사용자 2026-09-29).
			if (!this.tcp) {
				fields.push({ type: 'static-text', id: CastikaInstance.fieldId('tcpnote', sf.id), width: 12, label: ' ',
					value: '**Switching pages needs Companion\'s TCP API, which is off: Settings > Protocols > TCP Listener.**',
					isVisibleExpression: `$(options:${mode}) == 'page'` })
			}
		}
		return fields
	}

	// -- adapter I/O --------------------------------------------------------------
	url(path) {
		return `http://${this.config.host || '127.0.0.1'}:${this.config.port || 18790}${path}`
	}

	async call(method, path, body) {
		const res = await fetch(this.url(path), {
			method,
			headers: { 'Content-Type': 'application/json' },
			body: body === undefined ? undefined : JSON.stringify(body),
			signal: AbortSignal.timeout(1500),
		})
		return res.json()
	}

	async pushSettings() {
		const c = this.config
		const settings = {
			show: c.show !== false,
			start_after: c.start_after,
			style: c.style,
			text: c.text,
			font: c.font,
			color: c.color,
			color_low: hex(c.color_low),
			color_mid: hex(c.color_mid),
			color_high: hex(c.color_high),
			bars: c.bars,
			rotate: c.rotate,
			sensitivity: c.sensitivity,
			gate_ms: c.gate_ms,
			peak: c.peak !== false,
			stop_on_activity: c.stop_on_activity !== false,
			analysis: c.analysis,
			// per deck: "off" | "keys" | page number
			surfaces: Object.fromEntries(this.surfaces.map((sf) => {
				const mode = c[CastikaInstance.fieldId('mode', sf.id)] || 'keys'
				return [sf.id, mode === 'page' ? Number(c[CastikaInstance.fieldId('page', sf.id)]) || 1 : mode]
			})),
		}
		try {
			await this.call('POST', '/settings', settings)
			this.setReachable(true)
		} catch (e) {
			this.setReachable(false, e)
		}
	}

	scheduleKeys() {
		clearTimeout(this.keysTimer)
		this.keysTimer = setTimeout(() => this.pushKeys(), KEYS_DEBOUNCE_MS)
	}

	async pushKeys() {
		const keys = []
		for (const [id, k] of this.keys) keys.push({ id, page: k.page, row: k.row, col: k.col, size: k.size })
		try {
			await this.call('POST', '/keys', { keys })
			this.setReachable(true)
		} catch (e) {
			this.setReachable(false, e)
		}
	}

	schedulePoll(ms) {
		clearTimeout(this.pollTimer)
		if (!this.destroyed) this.pollTimer = setTimeout(() => this.poll(), ms)
	}

	async poll() {
		let data
		try {
			data = await this.call('GET', `/frames?seq=${this.seq}`)
			this.setReachable(true)
		} catch (e) {
			this.setReachable(false, e)
			this.schedulePoll(POLL_DOWN_MS)
			return
		}
		this.schedulePoll(data.running ? POLL_MS : POLL_IDLE_MS)
		if (data.boot && data.boot !== this.boot) {
			// The installation restarted: it starts from its own settings file and
			// knows no buttons, so hand it this connection's settings and buttons again.
			const first = this.boot === null
			this.boot = data.boot
			if (!first) this.log('info', 'Castika DeckShow restarted: sending the settings and buttons again')
			this.pushSettings()
			this.scheduleKeys()
		}
		this.seq = data.seq
		const changed = Object.keys(data.images || {})
		for (const id of changed) this.images.set(id, data.images[id])
		if (data.running !== this.running) {
			this.running = data.running
			if (!this.running) this.images.clear()
			this.checkFeedbacks('show_key')
		} else if (changed.length) {
			this.checkFeedbacksById(...changed)
		}
	}

	// A deck set to "Show on a specific page and back" cannot switch pages while
	// Companion's TCP API is off, and nothing on screen said so (사용자 2026-09-29).
	needsTcp() {
		if (this.tcp) return false
		return this.surfaces.some((sf) => this.config[CastikaInstance.fieldId('mode', sf.id)] === 'page')
	}

	showStatus() {
		if (this.reachable === false) return
		if (this.needsTcp()) {
			this.updateStatus(InstanceStatus.BadConfig, 'Page switching needs Settings > Protocols > TCP Listener')
		} else {
			this.updateStatus(InstanceStatus.Ok)
		}
	}

	setReachable(ok, err) {
		if (ok === this.reachable) return
		this.reachable = ok
		if (ok) {
			this.showStatus()
			this.pushSettings() // the adapter may have restarted: tell it the form again
			this.scheduleKeys() // ... and which buttons we placed
		} else {
			this.updateStatus(InstanceStatus.ConnectionFailure, 'Castika DeckShow adapter not reachable: ' + (err?.message || err))
		}
	}

	// -- definitions -------------------------------------------------------------------
	actions() {
		return {
			press: {
				name: 'Toggle DeckShow',
				options: [],
				callback: async (action) => {
					try {
						await this.call('POST', '/press', { id: action.controlId })
					} catch (e) {
						this.setReachable(false, e)
					}
				},
			},
			// The long press of the preset, and an action to put on a button of
			// your own: no more idle starts until a button is pressed. There is no
			// action for the way back: Toggle DeckShow (any press) already does it.
			session_off: {
				name: 'Temporary Off',
				options: [],
				callback: async () => {
					try {
						await this.call('POST', '/session', { off: true })
					} catch (e) {
						this.setReachable(false, e)
					}
				},
			},
		}
	}

	feedbacks() {
		return {
			show_key: {
				type: 'advanced',
				name: 'Show on this button',
				description: 'The show is drawn on this button while it runs. Placed automatically by the preset.',
				affectedProperties: ['png64'],
				options: [
					{
						type: 'textinput',
						id: 'location',
						label: 'Where this button is (filled in automatically)',
						default: '$(this:page)/$(this:row)/$(this:column)',
						useVariables: true,
					},
				],
				callback: (feedback) => {
					this.rememberKey(feedback)
					const png64 = this.running ? this.images.get(feedback.id) : null
					return png64 ? { png64 } : {}
				},
				unsubscribe: (feedback) => {
					if (this.keys.delete(feedback.id)) this.scheduleKeys()
				},
			},
		}
	}

	rememberKey(feedback) {
		const [page, row, col] = String(feedback.options.location || '')
			.split('/')
			.map((x) => parseInt(x, 10))
		if (![page, row, col].every(Number.isInteger)) return
		const size = feedback.image?.width || 72
		const prev = this.keys.get(feedback.id)
		if (prev && prev.page === page && prev.row === row && prev.col === col && prev.size === size) return
		this.keys.set(feedback.id, { page, row, col, size, controlId: feedback.controlId })
		this.scheduleKeys()
	}

	presets() {
		const presets = {
			show_key: {
				type: 'simple',
				name: 'DeckShow Button',
				style: { text: '', size: 'auto', color: combineRgb(255, 255, 255), bgcolor: combineRgb(0, 0, 0), show_topbar: false, png64: KEY_ICON },
				// Short press toggles, holding it for 1.5 s stops the show for this
				// session, the same as a long press on a Stream Deck button. With a
				// duration group present, "up" is the short-press set.
				steps: [
					{
						down: [],
						up: [{ actionId: 'press', options: {} }],
						1500: { options: { runWhileHeld: true }, actions: [{ actionId: 'session_off', options: {} }] },
					},
				],
				feedbacks: [{ feedbackId: 'show_key', options: { location: '$(this:page)/$(this:row)/$(this:column)' } }],
			},
		}
		const structure = [{ id: 'show', name: 'Castika DeckShow', definitions: ['show_key'] }]
		return [structure, presets]
	}
}

// Companion 5 loads the bundle with ESM import() and needs `default` to be the
// class itself and `UpgradeScripts` next to it (ConnectionThread.js). Node's
// CJS interop gives it module.exports as `default`, so module.exports must BE
// the class -- hence CommonJS here, not `export default`.
CastikaInstance.UpgradeScripts = []
module.exports = CastikaInstance
