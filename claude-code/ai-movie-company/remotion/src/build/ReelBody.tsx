import {AbsoluteFill, Img, OffthreadVideo, staticFile, useCurrentFrame} from 'remotion';
import type {CSSProperties} from 'react';
import type {RenderBox, RenderEffect, RenderReelBody} from '../buildTypes';
import rules from './animation.json';

type PictureEffect = Extract<RenderEffect, {type: 'zoom' | 'shake'}>;
type GraphicEffect = Exclude<RenderEffect, PictureEffect>;

const position = (box: RenderBox): CSSProperties => ({
	position: 'absolute', left: box.x, top: box.y, width: box.width, height: box.height,
});

const phase = (effect: RenderEffect, frame: number) => {
	const steps = effect.endFrame - effect.startFrame - 1;
	return steps ? (frame - effect.startFrame) / steps : 0.5;
};

const pictureTransform = (effect: PictureEffect, frame: number, width: number, height: number) => {
	const t = phase(effect, frame);
	const swell = Math.sin(t * Math.PI);
	if (effect.type === 'zoom') {
		return `scale(${1 + (effect.scale - 1) * swell})`;
	}
	const {amplitudePx} = effect;
	const x = amplitudePx * swell * Math.sin(t * 8 * Math.PI);
	const y = amplitudePx * swell * Math.cos(t * 6 * Math.PI);
	const scale = 1 + (2 * amplitudePx * swell) / Math.min(width, height);
	return `translate(${x}px, ${y}px) scale(${scale})`;
};

const Graphic: React.FC<{effect: GraphicEffect; frame: number; brand: RenderReelBody['brand']}> = ({
	effect, frame, brand,
}) => {
	const t = phase(effect, frame);
	const {box} = effect;
	const degrees = rules[`${effect.type}Degrees`] * Math.cos(Math.PI * t);
	const scale = effect.type === 'sticker' ? 1 + (rules.stickerScale - 1) * Math.sin(Math.PI * t) : 1;
	const direction = effect.slot === 'right' ? -1 : 1;
	const x = effect.type === 'flyby' ? direction * (2 * t - 1) * box.width * rules.flybyTravelWidths : 0;
	const style: CSSProperties = {
		...position(box), transform: `translateX(${x}px) rotate(${degrees}deg) scale(${scale})`,
		overflow: 'hidden',
	};
	if (effect.type !== 'tag') {
		return <Img src={staticFile(effect.asset)} style={{...style, objectFit: 'contain'}} />;
	}
	return (
		<div style={{
			...style, display: 'grid', placeItems: 'center', background: brand.accentColor,
			borderRadius: box.height * 0.18, color: brand.plateColor, textAlign: 'center',
			fontWeight: 900, whiteSpace: 'nowrap',
			fontSize: Math.min(box.height * 0.65, box.width * 0.9 / Array.from(effect.text).length),
		}}>
			{effect.text}
		</div>
	);
};

export const BuildReelBody: React.FC<RenderReelBody> = (props) => {
	const {video, width, height, effects, brand, captions, captionStyle, showPlate} = props;
	const frame = useCurrentFrame();
	const active = effects.filter((effect) => effect.startFrame <= frame && frame < effect.endFrame);
	const picture = active.find((effect): effect is PictureEffect => effect.type === 'zoom' || effect.type === 'shake');
	const page = captions.find((caption) => caption.startFrame <= frame && frame < caption.endFrame);
	const size = width * captionStyle.fontFrac;
	const half = size * rules.captionLineHeight;
	const margin = Math.min(width, height) * rules.plateMarginFraction;
	const plateWidth = width * rules.plateWidthFraction;
	const plateHeight = height * rules.plateHeightFraction;

	return (
		<AbsoluteFill style={{backgroundColor: '#000', overflow: 'hidden', fontFamily: brand.fontFamily}}>
			<AbsoluteFill style={{transform: picture && pictureTransform(picture, frame, width, height)}}>
				{video ? <OffthreadVideo src={staticFile(video)} style={{width, height, objectFit: 'fill'}} /> : null}
			</AbsoluteFill>
			{active.filter((effect): effect is GraphicEffect => 'box' in effect)
				.map((effect) => <Graphic key={effect.type} effect={effect} frame={frame} brand={brand} />)}
			{page ? (
				<div style={{
					position: 'absolute', left: '6%', right: '6%', top: captionStyle.yFrac * height - half,
					height: 2 * half, overflow: 'hidden', display: 'grid', placeItems: 'center',
					textAlign: 'center', fontWeight: 900, color: brand.textColor,
					fontSize: Math.min(size, width * 1.55 / Array.from(page.text).length),
					lineHeight: rules.captionLineHeight, overflowWrap: 'anywhere',
				}}>
					<span style={{
						backgroundColor: captionStyle.plate ? brand.plateColor : undefined,
						WebkitTextStroke: captionStyle.plate ? undefined : `${size * 0.06}px ${brand.plateColor}`,
						paintOrder: 'stroke fill',
					}}>
						{page.mode === 'phrase' ? page.text : page.words.map((word, i) => (
							<span key={i} style={{
								color: word.startFrame <= frame && frame < word.endFrame ? brand.accentColor : undefined,
							}}>
								{i ? ' ' : ''}{word.text.trim()}
							</span>
						))}
					</span>
				</div>
			) : null}
			{showPlate && brand.channelPlate ? (
				<div style={{
					position: 'absolute', right: margin, top: margin, width: plateWidth, height: plateHeight,
					display: 'grid', placeItems: 'center', overflow: 'hidden', whiteSpace: 'nowrap',
					backgroundColor: brand.plateColor, color: brand.textColor, fontWeight: 700,
					fontSize: Math.min(plateHeight * 0.55, plateWidth * 0.9 / Array.from(brand.channelPlate).length),
				}}>
					{brand.channelPlate}
				</div>
			) : null}
		</AbsoluteFill>
	);
};
