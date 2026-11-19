// Options here apply only to CLI commands (studio, render, still). The pipeline passes everything
// job-specific on the command line (--props, --public-dir, codec flags).
// https://www.remotion.dev/docs/config
import fs from 'node:fs';
import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
// Limited-range BT.709 (yuv420p, tagged): full-range yuvj420p can shift contrast on
// Instagram/Facebook (donor ~/Code/remotion).
Config.setColorSpace('bt709');

// Render with the installed Google Chrome instead of downloading Chrome Headless Shell
// (the fallback download is not hash-checked in older 4.0.x — see
// docs/npm-supply-chain-audit-2026-11-18.md). Without Chrome, Remotion's default applies.
const SYSTEM_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
if (fs.existsSync(SYSTEM_CHROME)) {
	Config.setBrowserExecutable(SYSTEM_CHROME);
	Config.setChromeMode('chrome-for-testing');
}
