import type {Caption} from '@remotion/captions';

type DisplayPage = {
	from: number;
	to: number;
	visibleFrom: number;
	whole: boolean;
	count: number;
};

type ScheduledToken = Caption & {
	displayPage?: DisplayPage;
	highlightFrames?: [number, number];
};

export const scheduledPages = (captions: ScheduledToken[]) =>
	captions.flatMap(({displayPage}, i) => displayPage ? [{
		...displayPage,
		tokens: captions.slice(i, i + displayPage.count),
	}] : []);

export const isHighlighted = ({highlightFrames}: ScheduledToken, frame: number): boolean =>
	highlightFrames !== undefined && highlightFrames[0] <= frame && frame < highlightFrames[1];

export const pageOpacity = ({from, to, visibleFrom}: DisplayPage, frame: number, pop: number): number => {
	if (frame < visibleFrom || frame >= to) {
		return 0;
	}
	return visibleFrom === from ? 1 : Math.min(1, pop * 1.4);
};
