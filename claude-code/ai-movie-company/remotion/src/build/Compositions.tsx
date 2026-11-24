import {Composition, type CalculateMetadataFunction} from 'remotion';
import {DEFAULT_BRAND} from '../theme';
import type {RenderFrame, RenderLongCard, RenderLongOverlay, RenderReelBody, RenderReelCard} from '../buildTypes';
import {BuildReelCard, BuildLongCard} from './Cards';
import {BuildLongOverlay} from './LongOverlay';
import {BuildReelBody} from './ReelBody';

type Props<T extends RenderFrame> = {[K in keyof T]: T[K]};

const register = <T extends RenderFrame,>(id: string, component: React.FC<Props<T>>, defaults: Props<T>) => {
	const calculateMetadata: CalculateMetadataFunction<Props<T>> =
		({props: {width, height, fps, durationInFrames}}) => ({width, height, fps, durationInFrames});
	const {width, height, fps, durationInFrames} = defaults;
	return (
		<Composition id={id} component={component} defaultProps={defaults} calculateMetadata={calculateMetadata}
			width={width} height={height} fps={fps} durationInFrames={durationInFrames} />
	);
};

const reel = {width: 1080, height: 1920, fps: 25, durationInFrames: 75};
const long = {width: 1920, height: 1080, fps: 25, durationInFrames: 100};

const body: RenderReelBody = {
	...reel, kind: 'reel_body', video: '', layout: 'medium', brand: DEFAULT_BRAND, showPlate: false,
	captions: [], captionStyle: {fontFrac: 0.085, yFrac: 0.76, plate: false}, effects: [],
};
const reelCard: RenderReelCard = {
	...reel, kind: 'reel_card', role: 'intro', text: 'Intro', wordRevealFrames: [0],
	brand: DEFAULT_BRAND, showPlate: false,
};
const longCard: RenderLongCard = {
	...long, kind: 'long_card', role: 'outro', video: null, title: 'Outro', points: [], brand: DEFAULT_BRAND,
};
const overlay: RenderLongOverlay = {
	...long, kind: 'long_overlay', video: null, brand: DEFAULT_BRAND, overlays: [],
};

export const BuildCompositions: React.FC = () => (
	<>
		{register<RenderReelBody>('BuildReelBody', BuildReelBody, body)}
		{register<RenderReelCard>('BuildReelCard', BuildReelCard, reelCard)}
		{register<RenderLongCard>('BuildLongCard', BuildLongCard, longCard)}
		{register<RenderLongOverlay>('BuildLongOverlay', BuildLongOverlay, overlay)}
	</>
);
