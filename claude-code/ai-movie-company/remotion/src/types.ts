import type {Caption} from '@remotion/captions';

/**
 * Every composition is driven only by props. The pipeline (Python, vp.adapters.render_remotion)
 * writes them as JSON and passes `--props`; nothing in these files knows about a job.
 * Brand values are props too (SPEC A4: brand = parameters, never code).
 */
export type Brand = {
	fontFamily: string;
	textColor: string;
	plateColor: string;
	cardBg: string;
	cardTextColor: string;
	accentColor: string;
	/** Channel plate text, e.g. "@AlexKrol"; null = no plate. */
	channelPlate: string | null;
};

export type Frame = {
	width: number;
	height: number;
	fps: number;
	durationInFrames: number;
};

export type CaptionStyle = {
	/** word: the current word lights up (timing estimated by code when only phrase times exist);
	 *  phrase: the whole page appears at once. */
	highlight: 'word' | 'phrase';
	/** Text size as a fraction of the frame width. */
	fontFrac: number;
	/** Centre of the caption block, fraction of the frame height from the top. */
	yFrac: number;
	/** Dark rounded plate behind the page (true) or outline + shadow only (false). */
	plate: boolean;
};

export type BumperProps = Frame & {
	role: 'intro' | 'outro';
	text: string;
	brand: Brand;
	/** Show the channel plate (outro). */
	showPlate: boolean;
};

export type MainProps = Frame & {
	/** File name inside the public dir the pipeline passes (--public-dir); null = transparent
	 *  overlay only (captions/plate layer for an editor). */
	video: string | null;
	captions: Caption[];
	captionStyle: CaptionStyle;
	brand: Brand;
	showPlate: boolean;
};

export type CoverProps = {
	/** Frame picture inside the public dir. */
	frame: string;
	text: string;
	brand: Brand;
	/** Text colour already contrast-checked by code against brand.plateColor. */
	textOnPlate: string;
};
