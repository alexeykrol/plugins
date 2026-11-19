import {Easing, interpolate} from 'remotion';
import type {Brand, CaptionStyle} from './types';

/** Neutral stubs (SPEC A4); the pipeline always passes the configured brand. */
export const DEFAULT_BRAND: Brand = {
	fontFamily: "-apple-system, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif",
	textColor: '#FFFFFF',
	plateColor: '#000000',
	cardBg: '#14181F',
	cardTextColor: '#FFFFFF',
	accentColor: '#FFD23F',
	channelPlate: '@AlexKrol',
};

export const DEFAULT_CAPTION_STYLE: CaptionStyle = {
	highlight: 'word',
	fontFrac: 0.085,
	yFrac: 0.72,
	plate: false,
};

const EASE_OUT = Easing.bezier(0.16, 1, 0.3, 1);

/** Clamped, eased interpolation. */
export const ease = (
	frame: number,
	[start, end]: readonly [number, number],
	[from, to]: readonly [number, number],
): number =>
	interpolate(frame, [start, end], [from, to], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
		easing: EASE_OUT,
	});

/** Opacity that fades out over the last `frames` frames of a sequence of `total` frames. */
export const outFade = (frame: number, total: number, frames = 6): number =>
	interpolate(frame, [total - frames, total - 1], [1, 0], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});

/** '#RRGGBB' + alpha 0..1 -> rgba(). */
export const withAlpha = (hex: string, alpha: number): string => {
	const s = hex.replace('#', '');
	const r = parseInt(s.slice(0, 2), 16);
	const g = parseInt(s.slice(2, 4), 16);
	const b = parseInt(s.slice(4, 6), 16);
	return `rgba(${r},${g},${b},${alpha})`;
};
