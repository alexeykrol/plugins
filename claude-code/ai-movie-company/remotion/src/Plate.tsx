import {AbsoluteFill, Interactive} from 'remotion';
import type {Brand} from './types';

/** Channel plate, top-right, opaque — the same place the ffmpeg renderer draws it. */
export const Plate: React.FC<{brand: Brand; width: number; height: number}> = ({brand, width, height}) => {
	if (!brand.channelPlate) {
		return null;
	}
	const size = Math.max(10, Math.round(Math.min(width, height) * 0.045));
	const pad = Math.max(4, Math.round(size / 2));
	return (
		<AbsoluteFill style={{alignItems: 'flex-end', justifyContent: 'flex-start', padding: pad}}>
			<Interactive.Div
				name="Channel plate"
				style={{
					fontFamily: brand.fontFamily,
					fontSize: size,
					fontWeight: 700,
					lineHeight: 1.1,
					color: brand.textColor,
					backgroundColor: brand.plateColor,
					padding: `${Math.round(size / 4)}px ${Math.round(size / 2)}px`,
					borderRadius: Math.round(size / 4),
				}}
			>
				{brand.channelPlate}
			</Interactive.Div>
		</AbsoluteFill>
	);
};
