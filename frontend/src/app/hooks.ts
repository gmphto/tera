import { useDispatch, useSelector } from "react-redux";

import type { AppDispatch, RootState } from "./store";

/** The typed Redux hooks the shell uses. */
export const useAppDispatch = useDispatch.withTypes<AppDispatch>();
export const useAppSelector = useSelector.withTypes<RootState>();
