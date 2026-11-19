import {AbsoluteFill, OffthreadVideo, staticFile, useVideoConfig} from 'remotion';
import {Captions} from './Captions';
import {Plate} from './Plate';
import type {MainProps} from './types';

/**
 * The clip itself (cut.mp4 from the public dir the pipeline passes) with burned captions and the
 * channel plate. With `video: null` it is a transparent overlay — the captions/plate layer an
 * editor puts on its own track over the cut.
 */
export const Main: React.FC<MainProps> = ({video, captions, captionStyle, brand, showPlate}) => {
	const {width, height} = useVideoConfig();
	return (
		<AbsoluteFill style={{backgroundColor: video ? '#000000' : undefined}}>
			{video ? <OffthreadVideo src={staticFile(video)} /> : null}
			{captions.length > 0 ? <Captions captions={captions} style={captionStyle} brand={brand} /> : null}
			{showPlate ? <Plate brand={brand} width={width} height={height} /> : null}
		</AbsoluteFill>
	);
};
