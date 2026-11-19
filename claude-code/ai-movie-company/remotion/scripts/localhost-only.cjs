// Keeps Remotion Studio and the render server reachable only from this machine.
// Older 4.0.x builds bind both to all interfaces (:: / 0.0.0.0) with no --host flag; the
// pipeline loads this file via NODE_OPTIONS for every remotion command it starts.
// Origin: ~/Code/remotion/scripts/studio-localhost.cjs (measured 2026-10-22: *:3000 without it,
// 127.0.0.1:3000 with it).
const net = require('node:net');

const WILDCARD_HOSTS = new Set(['::', '0.0.0.0']);
const originalListen = net.Server.prototype.listen;

const isPort = (v) => typeof v === 'number' || (typeof v === 'string' && /^\d+$/.test(v));

// Every TCP form of listen() without a concrete host goes to 127.0.0.1: listen(port[, cb]),
// listen(port, '::' | '0.0.0.0'[, ...]), listen({port}) / listen({port, host: wildcard}).
// Unix sockets and handles (listen('/path'), listen({path}), listen(handle)) are left alone.
net.Server.prototype.listen = function (...args) {
	const [first] = args;
	if (first && typeof first === 'object' && 'port' in first && !('path' in first)) {
		if (first.host === undefined || WILDCARD_HOSTS.has(first.host)) {
			args[0] = {...first, host: '127.0.0.1'};
		}
	} else if (isPort(first) || first === undefined || typeof first === 'function') {
		if (typeof args[1] === 'string' && WILDCARD_HOSTS.has(args[1])) {
			args[1] = '127.0.0.1';
		} else if (typeof args[1] !== 'string') {
			// listen(), listen(cb), listen(port), listen(port, cb), listen(port, backlog, cb)
			const port = isPort(first) ? first : 0;
			const rest = isPort(first) ? args.slice(1) : args;
			args = [port, '127.0.0.1', ...rest];
		}
	}
	return originalListen.apply(this, args);
};
