import {AbsoluteFill, OffthreadVideo, staticFile, useCurrentFrame} from 'remotion';
import {Plate} from '../Plate';
import type {RenderLongCard, RenderReelCard} from '../buildTypes';

export const BuildReelCard: React.FC<RenderReelCard> = ({
	text, wordRevealFrames, subline, brand, showPlate, width, height,
}) => {
	const frame = useCurrentFrame();
	const size = Math.min(width, height) * 0.085;
	const words = text.trim().split(/\s+/);

	return (
		<AbsoluteFill style={{
			background: brand.cardBg, color: brand.cardTextColor, fontFamily: brand.fontFamily,
			display: 'grid', placeItems: 'center', textAlign: 'center',
		}}>
			<div style={{width: '86%', fontSize: size, fontWeight: 800, lineHeight: 1.18, textWrap: 'balance'}}>
				{words.map((word, i) => (
					<span key={i} style={{visibility: frame >= (wordRevealFrames[i] ?? 0) ? 'visible' : 'hidden'}}>
						{i ? ' ' : ''}{word}
					</span>
				))}
				<div style={{height: size * 0.10, width: '30%', background: brand.accentColor, margin: '0.65em auto 0'}} />
			</div>
			{subline && frame >= subline.revealFrame ? (
				<div style={{position: 'absolute', bottom: '12%', width: '86%', fontSize: size * 0.46}}>
					{subline.text}
				</div>
			) : null}
			{showPlate ? <Plate brand={brand} width={width} height={height} /> : null}
		</AbsoluteFill>
	);
};

export const BuildLongCard: React.FC<RenderLongCard> = ({
	role, video, title, subtitle, points, endText, subline, brand, height,
}) => {
	const frame = useCurrentFrame();
	return (
		<AbsoluteFill style={{backgroundColor: brand.cardBg, color: brand.cardTextColor, fontFamily: brand.fontFamily}}>
			{video ? <OffthreadVideo src={staticFile(video)} muted={role !== 'hook'}
				style={{width: '100%', height: '100%', objectFit: 'cover'}} /> : null}
			<AbsoluteFill style={{
				padding: '7%', justifyContent: 'center', gap: height * 0.018,
				background: video ? 'linear-gradient(90deg, #000c, #0002)' : undefined,
				textAlign: video ? 'left' : 'center',
			}}>
				<div style={{fontSize: height * 0.075, fontWeight: 800, textWrap: 'balance'}}>{title}</div>
				{subtitle ? <div style={{fontSize: height * 0.045}}>{subtitle}</div> : null}
				{points.map((point, i) => (
					<div key={i} style={{
						fontSize: height * 0.048, visibility: frame >= point.revealFrame ? 'visible' : 'hidden',
					}}>
						{point.text}
					</div>
				))}
				{[endText, subline].map((line, i) => line ? (
					<div key={i} style={{fontSize: height * 0.038, color: brand.accentColor,
						visibility: frame >= line.revealFrame ? 'visible' : 'hidden'}}>
						{line.text}
					</div>
				) : null)}
			</AbsoluteFill>
		</AbsoluteFill>
	);
};
