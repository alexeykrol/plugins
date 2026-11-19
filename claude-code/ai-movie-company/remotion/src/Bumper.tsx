import {AbsoluteFill, Interactive, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {Plate} from './Plate';
import {ease, outFade, withAlpha} from './theme';
import type {BumperProps} from './types';

/**
 * Intro / outro card: the S6a text appears word by word over the brand background, an accent
 * bar grows under it, everything fades out in the last frames. No audio (the pipeline adds
 * silence when it joins the pieces).
 */
export const Bumper: React.FC<BumperProps> = ({role, text, brand, showPlate}) => {
	const frame = useCurrentFrame();
	const {fps, width, height, durationInFrames} = useVideoConfig();
	const words = text.split(/\s+/).filter(Boolean);
	const size = Math.round(Math.min(width, height) * 0.085);
	// All words are in within ~45% of the card, whatever its length.
	const stagger = words.length > 1 ? Math.max(1, Math.floor((durationInFrames * 0.45) / words.length)) : 0;
	const barIn = spring({frame: frame - 4, fps, config: {damping: 200}});
	const fade = role === 'outro' ? 1 : outFade(frame, durationInFrames, 5);

	return (
		<AbsoluteFill
			style={{
				backgroundColor: brand.cardBg,
				backgroundImage: `radial-gradient(circle at 50% 38%, ${withAlpha(brand.accentColor, 0.16)}, transparent 62%)`,
				justifyContent: 'center',
				alignItems: 'center',
				flexDirection: 'column',
				gap: Math.round(size * 0.6),
				opacity: fade,
			}}
		>
			<Interactive.Div
				name={role === 'intro' ? 'Intro text' : 'Outro text'}
				style={{
					width: '86%',
					fontFamily: brand.fontFamily,
					fontSize: size,
					fontWeight: 800,
					lineHeight: 1.18,
					color: brand.cardTextColor,
					textAlign: 'center',
					textWrap: 'balance',
				}}
			>
				{words.map((w, i) => {
					const start = i * stagger;
					return (
						<span
							key={`${i}-${w}`}
							style={{
								display: 'inline-block',
								whiteSpace: 'pre',
								opacity: ease(frame, [start, start + 8], [0, 1]),
								translate: `0px ${ease(frame, [start, start + 10], [size * 0.45, 0])}px`,
							}}
						>
							{i > 0 ? ' ' : ''}
							{w}
						</span>
					);
				})}
			</Interactive.Div>
			<Interactive.Div
				name="Accent bar"
				style={{
					height: Math.max(4, Math.round(size * 0.1)),
					width: `${Math.round(barIn * 28)}%`,
					borderRadius: 999,
					backgroundColor: brand.accentColor,
				}}
			/>
			{showPlate ? <Plate brand={brand} width={width} height={height} /> : null}
		</AbsoluteFill>
	);
};
