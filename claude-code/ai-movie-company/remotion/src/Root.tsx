import {Composition, type CalculateMetadataFunction} from 'remotion';
import {Bumper} from './Bumper';
import {Cover} from './Cover';
import {Main} from './Main';
import {DEFAULT_BRAND, DEFAULT_CAPTION_STYLE} from './theme';
import type {BumperProps, CoverProps, Frame, MainProps} from './types';

// Size, fps and length come from the props the pipeline writes (the cut's own values), so one
// composition serves 9:16 reels and 16:9 / 21:9 long videos alike.
function fromProps<T extends Frame>(): CalculateMetadataFunction<T> {
	return ({props}) => ({
		width: props.width,
		height: props.height,
		fps: props.fps,
		durationInFrames: Math.max(1, props.durationInFrames),
	});
}

const REEL: Frame = {width: 810, height: 1440, fps: 25, durationInFrames: 100};

const bumperDefaults: BumperProps = {
	...REEL,
	durationInFrames: 38,
	role: 'intro',
	text: 'Почему все изменения ботов должны идти через одну точку',
	brand: DEFAULT_BRAND,
	showPlate: false,
};

const mainDefaults: MainProps = {
	...REEL,
	video: null,
	captions: [
		{text: 'Один', startMs: 0, endMs: 400, timestampMs: 200, confidence: null},
		{text: ' бот', startMs: 400, endMs: 800, timestampMs: 600, confidence: null},
		{text: ' правит', startMs: 800, endMs: 1300, timestampMs: 1050, confidence: null},
		{text: ' всеми', startMs: 1300, endMs: 1900, timestampMs: 1600, confidence: null, pageBreakAfter: true},
		{text: ' другими', startMs: 2000, endMs: 2600, timestampMs: 2300, confidence: null},
		{text: ' ботами', startMs: 2600, endMs: 3300, timestampMs: 2950, confidence: null},
	],
	captionStyle: DEFAULT_CAPTION_STYLE,
	brand: DEFAULT_BRAND,
	showPlate: true,
};

const coverDefaults: CoverProps = {
	frame: 'frame.png',
	text: 'Один бот правит всеми',
	brand: DEFAULT_BRAND,
	textOnPlate: '#FFFFFF',
};

export const RemotionRoot: React.FC = () => {
	return (
		<>
			<Composition
				id="Bumper"
				component={Bumper}
				defaultProps={bumperDefaults}
				calculateMetadata={fromProps<BumperProps>()}
				{...REEL}
			/>
			<Composition
				id="Main"
				component={Main}
				defaultProps={mainDefaults}
				calculateMetadata={fromProps<MainProps>()}
				{...REEL}
			/>
			<Composition
				id="Cover"
				component={Cover}
				defaultProps={coverDefaults}
				width={1280}
				height={720}
				fps={25}
				durationInFrames={1}
			/>
		</>
	);
};
