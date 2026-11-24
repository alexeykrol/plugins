/** Local builder v1 props. All intervals are half-open composition-frame ranges. */

export interface RenderFrame {
	width: number;
	height: number;
	fps: number;
	durationInFrames: number;
}

export interface RenderBrand {
	fontFamily: string;
	textColor: string;
	plateColor: string;
	cardBg: string;
	cardTextColor: string;
	accentColor: string;
	channelPlate: string | null;
}

export interface RenderBox {
	x: number;
	y: number;
	width: number;
	height: number;
}

export interface RenderCaptionStyle {
	fontFrac: number;
	yFrac: number;
	plate: boolean;
}

export interface RenderCaptionWord {
	text: string;
	startFrame: number;
	endFrame: number;
}

export interface RenderCaptionPage {
	text: string;
	mode: 'word' | 'phrase';
	startFrame: number;
	endFrame: number;
	words: RenderCaptionWord[];
}

interface _RenderEffectSpan {
	startFrame: number;
	endFrame: number;
}

export interface RenderTag extends _RenderEffectSpan {
	type: 'tag';
	text: string;
	slot: 'upper' | 'lower';
	box: RenderBox;
}

export interface RenderSticker extends _RenderEffectSpan {
	type: 'sticker';
	asset: string;
	slot: 'left' | 'right';
	box: RenderBox;
}

export interface RenderFlyby extends _RenderEffectSpan {
	type: 'flyby';
	asset: string;
	slot: 'left' | 'right';
	box: RenderBox;
}

export interface RenderZoom extends _RenderEffectSpan {
	type: 'zoom';
	scale: number;
}

export interface RenderShake extends _RenderEffectSpan {
	type: 'shake';
	amplitudePx: number;
}

export type RenderEffect = RenderTag | RenderSticker | RenderFlyby | RenderZoom | RenderShake;

export interface RenderCardPoint {
	text: string;
	revealFrame: number;
}

export interface RenderReelBody extends RenderFrame {
	kind: 'reel_body';
	video: string;
	brand: RenderBrand;
	showPlate: boolean;
	layout: 'medium' | 'split';
	captions: RenderCaptionPage[];
	captionStyle: RenderCaptionStyle;
	effects: RenderEffect[];
}

export interface RenderReelCard extends RenderFrame {
	kind: 'reel_card';
	role: 'intro' | 'thought';
	text: string;
	wordRevealFrames: number[];
	subline?: RenderCardPoint;
	brand: RenderBrand;
	showPlate: boolean;
}

export interface RenderOverlay {
	id: string;
	kind: 'chapter' | 'subscribe';
	text: string;
	/** Shift the full animation interval to chunk-local coordinates to preserve its phase. */
	startFrame: number;
	endFrame: number;
	box: RenderBox;
}

export interface RenderLongOverlay extends RenderFrame {
	kind: 'long_overlay';
	video: string | null;
	brand: RenderBrand;
	overlays: RenderOverlay[];
}

export interface RenderLongCard extends RenderFrame {
	kind: 'long_card';
	role: 'hook' | 'outro' | 'timelapse';
	video: string | null;
	title: string;
	subtitle?: string;
	endText?: RenderCardPoint;
	points: RenderCardPoint[];
	subline?: RenderCardPoint;
	brand: RenderBrand;
}

export type RendererProps = RenderReelBody | RenderReelCard | RenderLongOverlay | RenderLongCard;
