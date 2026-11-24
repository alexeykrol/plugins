import {AbsoluteFill, OffthreadVideo, staticFile, useCurrentFrame} from 'remotion';
import type {RenderLongOverlay} from '../buildTypes';

export const BuildLongOverlay: React.FC<RenderLongOverlay> = ({video, brand, overlays}) => {
	const frame = useCurrentFrame();
	return (
		<AbsoluteFill>
			{video ? <OffthreadVideo src={staticFile(video)} style={{width: '100%', height: '100%'}} /> : null}
			{overlays.filter(({startFrame, endFrame}) => startFrame <= frame && frame < endFrame).map((overlay) => {
				const {box, startFrame, endFrame} = overlay;
				const length = endFrame - startFrame;
				const ramp = Math.max(1, Math.round(length / 8));
				const opacity = Math.min(1, (frame - startFrame + 1) / ramp, (endFrame - frame) / ramp);
				return (
					<div key={overlay.id} style={{
						position: 'absolute', left: box.x, top: box.y, width: box.width, height: box.height,
						opacity, overflow: 'hidden', display: 'grid', placeItems: 'center',
						fontFamily: brand.fontFamily, fontWeight: 800, whiteSpace: 'nowrap',
						fontSize: Math.min(box.height * 0.58, box.width * 0.9 / Array.from(overlay.text).length),
						background: overlay.kind === 'chapter' ? brand.plateColor : brand.accentColor,
						color: overlay.kind === 'chapter' ? brand.textColor : brand.plateColor,
					}}>
						{overlay.text}
					</div>
				);
			})}
		</AbsoluteFill>
	);
};
